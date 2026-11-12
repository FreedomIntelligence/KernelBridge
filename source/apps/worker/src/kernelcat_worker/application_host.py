"""Local durable application host. Terminal clients never own executor lifetime.

The socket/control plane has its own threads; one dedicated thread owns the
finite Executor RPC scope. A slow model/device request cannot block pause or
input acknowledgement. Socket loss never causes an effect to be resubmitted.
"""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import Future
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.models import Conflict, Task, canonical
from kernelcat_executor.state.application import ApplicationJournal, ModelAdmissionStopped
from kernelcat_worker.bindings import build
from kernelcat_worker.configuration import load_config
from kernelcat_worker.startup import admit_config
from kernelcat_worker.application_artifacts import evaluator_identity, verify_evaluator, source_snapshot, restore_source, persist, read_regular, read_source_snapshot
from kernelcat_executor.generated.application_host import SCHEMA as HOST_SCHEMA

REQUEST_VALIDATOR = Draft202012Validator(HOST_SCHEMA)

MAX_FRAME = 16 * 1024 * 1024


def delegation_context(method, params, context):
    """Attribute delegate/child effects to their own Pi call, not the last bound one.

    Concurrent delegate calls share one worker connection; its bound context
    names whichever tool call Pi announced last.
    """
    from kernelcat_executor.contracts.models import digest
    if method == "plugin_begin" and isinstance(params.get("id"), str):
        return {**context, "tool_call_id": params["id"], "tool_name": "delegate",
                "tool_arguments": params.get("arguments")}
    if method in {"child_model", "child_tool", "plugin_settle"} and isinstance(params.get("permit"), str):
        trimmed = {key: value for key, value in context.items() if key != "tool_arguments"}
        return {**trimmed, "tool_call_id": "kc_" + digest(params["permit"]), "tool_name": "delegate"}
    return context


def peer_uid(connection: socket.socket) -> int:
    if sys.platform.startswith("linux"):
        return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.CDLL(None, use_errno=True).getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError(ctypes.get_errno(), "getpeereid failed")
        return uid.value
    raise RuntimeError("application host requires Linux or macOS (Windows: WSL2)")


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.resolve() != path or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Conflict("host directory must be a canonical user-owned directory with mode 0700")


def private_file(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


class ApplicationHost:
    def __init__(self, descriptor: dict, *, expected_modules=None):
        self.descriptor = descriptor
        self.identity = descriptor["application_session_id"]
        self.directory = Path(descriptor["host_directory"])
        private_directory(self.directory)
        self.lock = os.open(self.directory / "host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock_info = os.fstat(self.lock)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077:
            os.close(self.lock)
            raise Conflict("unsafe host lock permissions")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            raise Conflict("an active host holds this session; attach to its original socket") from None
        self.socket_path = self.directory / "host.sock"
        if len(os.fsencode(self.socket_path)) > 100:
            raise ValueError("host socket path is too long")
        token_path = self.directory / "token"
        if token_path.exists():
            info = token_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Conflict("unsafe host token permissions")
            token_path.unlink()
        self.token = secrets.token_hex(32)
        private_file(token_path, self.token.encode())
        self.tickets: dict[str, dict] = {}
        self.clients: set[Connection] = set()
        self.mutex = threading.RLock()
        self.effects: queue.Queue = queue.Queue()
        self.ready = Future()
        self.stopping = threading.Event()
        self.user_effects = {}
        self.recovered_epoch = 0
        self.expected_modules = expected_modules
        self.effect_thread = threading.Thread(target=self.executor, name="application-executor", daemon=True)
        self.effect_thread.start()
        self.ready.result(timeout=90)

    def executor(self):
        owner = scope = None
        handle = None
        try:
            config_path = Path(self.descriptor["config"])
            config = load_config(config_path)
            startup = admit_config(config, expected_modules=self.expected_modules)
            config = startup.bind_config(config)
            task = Task.model_validate_json(Path(self.descriptor["task"]).read_text())
            from kernelcat_executor.capabilities.workspace_tools import FunctionTool
            from kernelcat_executor.contracts.models import ToolSpec
            transition = FunctionTool(ToolSpec(name="context_transition", credits=1,
                description="Request a fresh Trial with the same source, or fork a source-bound historical entry. Takes effect after the current turn settles; budgets and user constraints are retained.",
                parameters={"type": "object", "properties": {"operation": {"enum": ["reset", "fork"]},
                    "entry_id": {"type": "string"}, "reason": {"type": "string", "minLength": 1, "maxLength": 4096}},
                    "required": ["operation", "reason"], "additionalProperties": False}), self.request_context_transition)
            finish_goal = FunctionTool(ToolSpec(name="finish_goal", credits=0,
                description="For an explicitly active codegen Goal only: report completion after doing and checking the requested work. Include the checks and any limits. This does not certify device acceptance.",
                parameters={"type": "object", "properties": {"summary": {"type": "string", "minLength": 1, "maxLength": 4096}},
                            "required": ["summary"], "additionalProperties": False}), self.finish_codegen_goal)
            agents_status = FunctionTool(ToolSpec(name="agents_status", credits=0,
                description="List this session's subagents with status, progress and (when finished) their result summaries. Use it to check background subagents.",
                parameters={"type": "object", "properties": {"agent_id": {"type": "string"}}, "additionalProperties": False}), self.agents_status)
            stop_agent = FunctionTool(ToolSpec(name="stop_agent", credits=0,
                description="Stop a running background subagent by agent_id. Its partial work is reported as a stopped result.",
                parameters={"type": "object", "properties": {"agent_id": {"type": "string", "minLength": 1}},
                            "required": ["agent_id"], "additionalProperties": False}), self.stop_agent)
            get_goal = FunctionTool(ToolSpec(name="get_goal", credits=0,
                description="Read the current explicit Goal and its receipt-derived progress and optional budget.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False}), self.get_codegen_goal)
            update_goal = FunctionTool(ToolSpec(name="update_goal", credits=0,
                description="Complete an active Goal only after verifying its objective, or report a concrete blocker needing user input. Pause/resume/clear remain user controls. Include evidence or the missing input in summary.",
                parameters={"type": "object", "properties": {"status": {"enum": ["complete", "blocked"]},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 4096}},
                    "required": ["status", "summary"], "additionalProperties": False}), self.update_codegen_goal)
            owner = build(config, config_path.parent, task, probe_versions=False, external_context=True, startup=startup,
                          subagent_home=Path(os.environ.get("KERNELBRIDGE_HOME") or Path.home() / ".kernelbridge"),
                          extra_tools=(finish_goal, get_goal, update_goal, agents_status, stop_agent) if config.workspace_mode == "project" else (transition,))
            # This host runs child model calls on their own threads, so
            # independent delegate calls from one reply may overlap.
            owner.concurrent_delegation = True
            owner.background_settled = self.background_settled
            owner.stopped_children = set()
            self.owner, self.task, self.config = owner, task, config
            self.evaluator = evaluator_identity(config, Path(self.descriptor["task"]))
            self.journal = ApplicationJournal(owner.gateway.store, self.identity)
            self.restarting = "task" in self.journal.state()
            self.journal.command("create", {"task": task.model_dump(mode="json"),
                "output": self.descriptor["output"], "goal": self.descriptor.get("goal"),
                "goal_defaults": self.descriptor["goal_defaults"],
                "workspace_mode": config.workspace_mode,
                "evaluator_sha256": self.evaluator["sha256"],
                "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()})
            # Crashed host leases are revoked only after holding the original OS lock.
            state = self.journal.state()
            if state["lease"]:
                self.journal.command("release", {**state["lease"], "reason": "host_recovery"})
            self.ready.set_result(True)
            while not self.stopping.is_set():
                item = self.effects.get()
                if item is None:
                    break
                connection, request, future = item
                if request.get("method") in {"child_model", "child_tool"} and handle is not None:
                    # A child model call waits on the provider. Run it on its own
                    # thread so concurrent children (and their reads) proceed;
                    # each child's requests stay ordered by its Pi lane.
                    threading.Thread(target=self.child_effect, args=(owner, handle, connection, request, future),
                                     name="application-child-model", daemon=True).start()
                    continue
                try:
                    method, params = request["method"], request.get("params", {})
                    if method == "close_scope":
                        for invocation in self.user_effects.values():
                            invocation.close()
                        self.user_effects.clear()
                        if scope:
                            scope.__exit__(None, None, None)
                            scope = handle = None
                        future.set_result({})
                        continue
                    self.require_worker(connection)
                    owner.gateway.store.bind_application_lease(self.identity, connection.worker["worker_id"], connection.worker["lease_epoch"])
                    app = self.journal.state()
                    admission_context = {**request.get("context", {}),
                        "call_id": request.get("call_id"), "task_version": app["task_version"],
                        "trial_id": app.get("trial_id"), "goal_id": (app["goal"] or {}).get("goal_id")}
                    if method == "application_user_bash":
                        accepted = app["inputs"].get(params["input_id"], {})
                        admission_context = {"input_id": params["input_id"], "initiator": "user", "tool_name": "bash",
                            "tool_arguments": {"command": params["command"]}, "task_version": app["task_version"],
                            "pi_session_id": accepted.get("pi_session_id"), "entry_id": accepted.get("entry_id"),
                            "call_id": request.get("call_id"),
                            "trial_id": app.get("trial_id"), "goal_id": (app["goal"] or {}).get("goal_id")}
                    if method in {"tool", "native_begin", "native_call", "batch", "application_user_bash", "rsi_adapter"}:
                        policy = owner.workspace_policy
                        frozen = self.capture_source()
                        persist(owner.gateway.store.path.parent / "checkpoints" / (frozen["source_sha256"] + ".json"), frozen)
                        admission_context.update(source_sha256=frozen["source_sha256"], evaluator_sha256=self.evaluator["sha256"])
                    admission_context = delegation_context(method, params, admission_context)
                    owner.gateway.store.bind_application_context(admission_context)
                    if method in {"application_recovery_plan", "application_recovery_commit"}:
                        from kernelcat_worker.application_recovery import recovery_plan, commit_recovery
                        from kernelcat_executor.contracts.models import digest
                        if method == "application_recovery_plan":
                            plan = recovery_plan(self, cancel_unstarted=True)
                            result = {**plan, "plan_digest": digest(plan)}
                        else:
                            result = commit_recovery(self, connection, params)
                            self.recovered_epoch = connection.worker["lease_epoch"]
                        future.set_result(result)
                        continue
                    if self.recovered_epoch != connection.worker["lease_epoch"]:
                        raise Conflict("recover original receipts before starting a new effect")
                    if handle is None:
                        scope = owner.open_rpc(task)
                        handle = scope.__enter__()
                    if method == "application_user_bash":
                        result = self.user_bash(connection, params)
                    elif method == "application_enqueue":
                        accepted = self.journal.state()["inputs"][params["input_id"]]
                        if accepted["kind"] == "control":
                            raise Conflict("control input cannot enter the model queue")
                        identity = owner.enqueue_input(task, accepted["text"],
                            "prompt" if accepted["kind"] == "policy" else accepted["kind"], accepted["input_id"])
                        result = {"executor_input_id": identity}
                    elif method == "application_stop_child":
                        result = self.stop_child(params.get("agent_id"))
                    elif method == "application_compact":
                        result = self.compact(params)
                    elif method == "application_admin":
                        operation = params["operation"]
                        if operation == "snapshot":
                            result = self.snapshot(connection, params["request"])
                        elif operation == "configure":
                            result = self.configure(connection, params["request"])
                        elif operation == "select_session":
                            result = self.select_session(connection, params["request"])
                        elif operation == "next":
                            result = self.next_input(connection)
                        else:
                            raise Conflict("unknown administrative operation")
                    else:
                        call_id = request["call_id"]
                        intent = {**connection.worker, "call_id": call_id, "method": method, "params": params,
                                  **request.get("context", {})}
                        original = self.journal.command("call_intent", intent)
                        if original.get("status") == "reconciled":
                            raise Conflict("original call was reconciled; use its retained checkpoint and receipts")
                        if original.get("status") == "completed":
                            result = original["result"]
                        elif original.get("status") == "pending":
                            raise Conflict("original call is unresolved; query its receipt instead of executing again")
                        else:
                            from kernelcat_executor.capabilities.streaming import observe
                            sequence = 0
                            def delta(value):
                                nonlocal sequence
                                sequence += 1
                                self.broadcast({"type": "model_delta", "generation_id": request.get("context", {}).get("model_generation_id", call_id),
                                                "sequence": sequence, "delta": value})
                            goal = self.journal.state()["goal"]
                            with observe(delta):
                                if method == "prepare_turn" and self.journal.state().get("takeover_requested"):
                                    result = {"stop": True, "result": {"status": "takeover"}}
                                elif method == "prepare_turn" and goal and goal["status"] not in {"active", "cleared"}:
                                    result = {"stop": True, "result": {"status": goal["status"]}}
                                elif method == "prepare_turn" and self.trial_exhausted():
                                    result = {"stop": True, "result": {"status": "trial_limit"}}
                                elif method == "prepare_turn" and any(item["status"] == "queued" and item.get("transition_operation") for item in self.journal.state()["inputs"].values()):
                                    result = {"stop": True, "result": {"status": "trial_transition"}}
                                else:
                                    if method in {"tool", "native_call", "batch"}:
                                        verify_evaluator(self.evaluator)
                                    try:
                                        result = handle(method, params)
                                    except ModelAdmissionStopped as stopped:
                                        if method != "model":
                                            raise
                                        # Pause may arrive after prepare_turn. The Journal
                                        # proves no model was admitted, so close only that
                                        # prepared turn and acknowledge this original call.
                                        interrupted = handle("interrupt_turn", {})
                                        if interrupted["needs_checkpoint"]:
                                            raise Conflict("admission stop unexpectedly has active effects") from stopped
                                        result = {"stop": True, "result": {"status": stopped.status}}
                                    if method == "hello":
                                        result["project_tools"] = [tool.name for tool in config.business_tools]
                                        result["subagents"] = {"max_concurrent": config.subagents.max_concurrent}
                                        provider = config.providers[owner.gateway.store.policy().routes[0]]
                                        if config.workspace_mode == "project":
                                            result["model_info"] = {"name": provider.model, "max_tokens": provider.max_tokens,
                                                "context_window": min(provider.context_window, config.policy.context_bytes // 8)}
                                        from kernelcat_worker.application_measurements import calibrate
                                        calibrate(self, connection)
                                    if method == "hello" and result.get("replay"):
                                        latest = self.journal.state().get("checkpoint", {})
                                        # Prefer a newer source/context checkpoint; a completed turn
                                        # after it remains authoritative in the original replay stream.
                                        end_ids = {key for key, item in owner.gateway.store.snapshot()["actions"].items() if item["action"]["tool"] == "__turn_end__"}
                                        ended = max((row["seq"] for row in owner.gateway.store._events(owner.gateway.store.events())
                                            if row["kind"] == "settled" and row.get("action_id") in end_ids), default=-1)
                                        if latest.get("native_checkpoint") and latest.get("checkpoint_seq", -1) > ended:
                                            result["replay"]["checkpoint"] = owner.replay.checkpoint(latest["native_checkpoint"])
                            self.journal.command("call_result", {"call_id": call_id, "result": result})
                            if method in {"end_turn", "tool", "native_settle", "rsi_apply"}:
                                from kernelcat_worker.application_acceptance import collect_acceptance
                                from kernelcat_worker.application_measurements import collect_paired
                                collect_acceptance(self)
                                collect_paired(self)
                            if method in {"prepare_turn", "model"} and result.get("stop") and goal and goal["status"] == "active":
                                outcome = result.get("result", {})
                                if outcome.get("status") not in {"finished", "trial_limit", "trial_transition", "takeover",
                                        "paused", "cleared", "awaiting_user", "completed", "failed"}:
                                    nested = outcome.get("result")
                                    rejection = nested.get("data") if isinstance(nested, dict) else None
                                    budget_rejected = isinstance(rejection, dict) and rejection.get("rejected") is True and rejection.get("reason") == "budget"
                                    status = "budget_limit" if budget_rejected or outcome.get("status") in {
                                        "step_limit", "budget_limit", "budget_exhausted", "budget_exhausted_during_update"
                                    } else "awaiting_user"
                                    self.journal.command("goal_outcome", {"status": status, "reason": outcome.get("status", "invocation_stopped")})
                    future.set_result(result)
                except BaseException as error:
                    future.set_exception(error)
                finally:
                    self.broadcast({"type": "state", "state": self.status()})
                    if request.get("method") in {"end_turn", "application_admin", "application_recovery_commit"}:
                        from kernelcat_worker.application_exports import export_application
                        try:
                            export_application(self)
                        except Exception as error:
                            self.broadcast({"type": "export_error", "message": str(error)})
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            else:
                self.broadcast({"type": "host_error", "error": str(error)})
        finally:
            if scope:
                scope.__exit__(None, None, None)
            if owner:
                owner.close()

    def child_effect(self, owner, handle, connection, request, future):
        """One child model call off the executor thread, with the same intent/result receipts."""
        try:
            method, params = request["method"], request.get("params", {})
            self.require_worker(connection)
            if self.recovered_epoch != connection.worker["lease_epoch"]:
                raise Conflict("recover original receipts before starting a new effect")
            app = self.journal.state()
            owner.gateway.store.bind_application_context(delegation_context(method, params, {**request.get("context", {}),
                "call_id": request.get("call_id"), "task_version": app["task_version"],
                "trial_id": app.get("trial_id"), "goal_id": (app["goal"] or {}).get("goal_id")}), local=True)
            call_id = request["call_id"]
            intent = {**connection.worker, "call_id": call_id, "method": method, "params": params,
                      **request.get("context", {})}
            original = self.journal.command("call_intent", intent)
            if original.get("status") == "reconciled":
                raise Conflict("original call was reconciled; use its retained checkpoint and receipts")
            if original.get("status") == "completed":
                result = original["result"]
            elif original.get("status") == "pending":
                raise Conflict("original call is unresolved; query its receipt instead of executing again")
            else:
                from kernelcat_executor.capabilities.streaming import observe
                sequence = 0
                def delta(value):
                    nonlocal sequence
                    sequence += 1
                    self.broadcast({"type": "model_delta", "generation_id": request.get("context", {}).get("model_generation_id", call_id),
                                    "sequence": sequence, "delta": value})
                with observe(delta):
                    result = handle(method, params)
            self.journal.command("call_result", {"call_id": call_id, "result": result})
            future.set_result(result)
        except BaseException as error:
            future.set_exception(error)
        finally:
            self.broadcast({"type": "state", "state": self.status()})

    def background_settled(self, action, outcome):
        """Announce a finished background child as a queued conversation input."""
        data = outcome.data
        label = action.arguments.get("description") or " ".join(action.arguments["task"].split())[:60]
        status = ("已完成" if outcome.status == "completed" and not data.get("partial") else
                  "部分完成" if outcome.status == "completed" else "已中断" if data.get("reason") == "worker_exited" else "失败")
        body = data.get("summary") or data.get("reason") or data.get("error") or ""
        text = (f"[子代理结果] {action.arguments['agent']}「{label}」{status}（agent_id: {action.id}）\n\n{body}").strip()[:65536]
        input_id = "subagent-" + uuid.uuid5(uuid.NAMESPACE_URL, action.id).hex
        if input_id in self.journal.state()["inputs"]:
            return  # Replay after result/notification crash window is idempotent.
        receipt = self.journal.command("accept", {"input_id": input_id,
            "text": text, "kind": "follow_up", "channel": "policy", "initiator": "agent"})
        self.broadcast({"type": "input", "receipt": receipt})

    def stop_child(self, agent_id):
        running = {child["id"] for child in self.status()["subagents"] if child["status"] == "running"}
        if agent_id not in running:
            raise Conflict("no running subagent with that agent_id")
        self.owner.stopped_children.add(agent_id)
        return {"agent_id": agent_id, "stopping": True}

    def agents_status(self, action):
        from kernelcat_executor.contracts.models import Outcome
        children = [{key: child.get(key) for key in ("id", "agent", "description", "status", "background", "partial",
                     "reason", "steps", "tool_calls", "summary", "started_at_ns", "ended_at_ns", "elapsed_ms")} for child in self.status()["subagents"]]
        wanted = action.arguments.get("agent_id")
        if wanted:
            children = [child for child in children if child["id"] == wanted]
        for child in children:
            child["summary"] = (child["summary"] or "")[:4000]
        return Outcome(data={"subagents": children})

    def stop_agent(self, action):
        from kernelcat_executor.contracts.models import Outcome
        try:
            return Outcome(data=self.stop_child(action.arguments["agent_id"]))
        except Conflict as error:
            return Outcome(status="failed", data={"error": str(error)})

    def user_bash(self, connection, params):
        # The native bash wrapper is executed by Pi with an Executor-owned intent.
        # Authorize only a previously accepted user command, never arbitrary tool calls.
        from kernelcat_executor.contracts.models import Action, digest
        from kernelcat_executor.operations.native import NativeEffects
        action_id = self.task.id + "/user-bash/" + digest(params["input_id"])
        accepted = self.journal.state()["inputs"].get(params["input_id"])
        if not accepted or accepted["initiator"] != "user" or accepted["text"].lstrip("!").strip() != params["command"]:
            raise Conflict("user bash does not match its accepted input")
        action = Action(id=action_id, task=self.task.id, tool="bash", arguments={"command": params["command"]})
        if "result" in params:
            invocation = self.user_effects.get(action_id)
            if invocation is None:
                raise Conflict("user bash has no original execution permit")
            result = params["result"]
            receipt = invocation.settle({"permit": action_id, "status": params["status"],
                "result": {"content": [{"type": "text", "text": result["output"]}],
                           "isError": params["status"] != "completed", "structuredContent": result}})
            del self.user_effects[action_id]
            return receipt
        if action_id in self.user_effects:
            raise Conflict("user bash is already running; retain its original permit")
        effects, calls = self.unsettled_foreground(self.status())
        if effects or calls:
            raise Conflict("user bash requires a settled foreground execution boundary")
        declaration = {"name": "bash", "description": "Run an admitted command in an isolated workspace snapshot",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                           "required": ["command"], "additionalProperties": False}}
        invocation = NativeEffects(self.owner.gateway, action, frozenset(), {"bash": declaration},
            lambda _: None, lambda *_: None, lambda: False, timeout=86400)
        self.user_effects[action_id] = invocation
        verify_evaluator(self.evaluator)
        return invocation.begin()

    def request_context_transition(self, action):
        from kernelcat_executor.contracts.models import Outcome, digest, ToolFailure
        if action.arguments["operation"] == "fork" and not action.arguments.get("entry_id"):
            raise ToolFailure("historical fork requires an exact source-bound entry ID")
        receipt = self.journal.command("accept", {"input_id": "agent-" + digest(action.id),
            "text": "Continue from the requested context transition. Reason: " + action.arguments["reason"],
            "kind": "policy", "initiator": "agent", "channel": "policy",
            "transition_operation": action.arguments["operation"], "target_entry_id": action.arguments.get("entry_id"),
            "action_id": action.id})
        return Outcome(data={"input_id": receipt["input_id"], "status": "queued", "terminate": True})

    def compact(self, params):
        from kernelcat_executor.operations.requests import propose
        from kernelcat_executor.contracts.harness import validate_replay
        harness = validate_replay(params["harness"], "HarnessInput")
        # Compaction has no tools and uses only the profile's admitted model.
        if harness.get("tools"):
            raise Conflict("compaction cannot request tools")
        policy = self.owner.gateway.store.policy()
        state = self.status()
        if state.get("takeover_requested"):
            raise Conflict("compaction is fenced while an interactive worker takes over")
        from kernelcat_executor.state.goal_progress import with_model_call_progress
        facts = with_model_call_progress(
            {key: state.get(key) for key in ("goal", "task_version", "checkpoint", "best", "platform", "budget", "pending_effect_ids")},
            self.owner.gateway.store)
        harness = {**harness, "system_prompt": harness["system_prompt"] + "\nRetain these authoritative facts verbatim in the summary:\n" + canonical(facts)}
        if len(canonical(harness).encode()) > policy.context_bytes:
            raise Conflict("compaction input and authoritative facts exceed the configured context budget")
        route = policy.routes[0]
        context = {"policy": policy.model_dump(mode="json"),
            **({"goal_progress": facts["goal_progress"]} if "goal_progress" in facts else {})}
        outcome = propose(self.owner.gateway, self.owner.proposers[route],
            self.task.id + "/compact/" + params["input_id"], self.task.id,
            route, context, (), (), harness=harness)
        if outcome.status != "completed":
            raise Conflict("compaction model did not complete; original reservation retained")
        blocks = outcome.data.get("content", [])
        if any(block.get("type") != "text" for block in blocks):
            raise Conflict("compaction returned an unexpected tool call")
        return {"text": "\n".join(block["text"] for block in blocks), "calls": [], "usage": outcome.data.get("usage", {})}

    def status(self):
        state = self.journal.state()
        state["host_pid"] = os.getpid()
        state["workspace_mode"] = self.config.workspace_mode
        state["subagent_config"] = self.config.subagents.model_dump()
        delegate = self.owner.gateway.tools.get("delegate")
        labels = {"read": "只读", "test": "可运行命令", "write": "独立 worktree 可写"}
        sources = {"builtin": "内置", "project": "项目（.kernelbridge/agents）", "user": "用户（~/.kernelbridge/agents）"}
        builtin = {"kernel-scout": "查找文件、搜索代码、追踪实现", "kernel-reviewer": "对改动或结论做独立复核",
                   "kernel-tester": "运行测试、构建和诊断，报告失败及证据", "kernel-worker": "在独立 Git worktree 分支中实现并验证改动"}
        state["subagent_types"] = [{"name": name, "description": builtin.get(name) if agent.get("source", "builtin") == "builtin" and name in builtin else agent["description"],
                                    "tools": labels.get(agent.get("tools", "read")),
                                    "source": sources.get(agent.get("source", "builtin"), "内置"), "prompt": agent.get("prompt", "")}
                                   for name, agent in (delegate.agents.items() if delegate is not None and hasattr(delegate, "agents") else ())]
        state["goal_review"] = next((item for item in reversed(state["configurations"])
            if item.get("operation") == "goal_review" and item.get("goal_id") == (state["goal"] or {}).get("goal_id")), None)
        state["fork_policy"] = (state.get("goal") or self.descriptor["goal_defaults"]).get("fork_policy", {})
        state["cwd"] = self.owner.workspace_policy["root"]
        route = self.owner.gateway.store.policy().routes[0]
        state["model"] = self.config.providers[route].model
        execution = self.owner.gateway.store.snapshot()
        state["budget"] = {key: execution.get(key) for key in ("limit", "spent", "reserved", "resources")}
        state["pending_effect_ids"] = [key for key, item in execution["actions"].items() if not item.get("terminal")]
        state["model_turns"] = sum(1 for item in execution["actions"].values() if item["action"]["tool"] == "__propose__")
        state["max_trial_turns"] = self.config.policy.max_steps
        from kernelcat_executor.contracts.models import digest
        child_activity = {}
        for item in execution["actions"].values():
            task_id = item["action"]["task"]
            if task_id.endswith("/leaf"):
                progress = child_activity.setdefault(task_id, {"steps": 0, "tool_calls": 0, "latest_tool": None,
                                                               "latest_detail": None, "tokens": 0})
                if item["action"]["tool"] == "__propose__":
                    progress["steps"] += 1
                    usage = item.get("outcome", {}).get("data", {}).get("usage") or {}
                    progress["tokens"] += sum(value for key, value in usage.items()
                                              if key in {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"}
                                              and isinstance(value, int))
                elif not item["action"]["tool"].startswith("__"):
                    progress["tool_calls"] += 1
                    progress["latest_tool"] = item["action"]["tool"]
                    arguments = item["action"]["arguments"]
                    progress["latest_detail"] = str(arguments.get("path") or arguments.get("query") or arguments.get("command")
                                                    or arguments.get("name") or "")[:120] or None
        def child_result(key, item):
            """A background child's own result supersedes its immediate 'started' receipt."""
            outcome = item.get("outcome", {})
            if outcome.get("data", {}).get("background"):
                return execution["actions"].get(key + "/background", {}).get("outcome") or {"status": "running", "data": {"background": True}}
            return outcome
        event_times = {}
        for event in self.owner.gateway.store._events(self.owner.gateway.store.events()):
            if event["kind"] == "admitted":
                event_times.setdefault(event["action"]["id"], {})["start"] = event.get("time_ns")
            elif event["kind"] in {"settled", "cancelled_unstarted"}:
                event_times.setdefault(event["action_id"], {})["end"] = event.get("time_ns")
        now = time.time_ns()
        def child_times(key, item):
            start = event_times.get(key, {}).get("start")
            background = item.get("outcome", {}).get("data", {}).get("background")
            end = event_times.get(key + "/background" if background else key, {}).get("end")
            # Nanoseconds exceed JavaScript's safe integer range. Preserve
            # exact timestamps as strings; elapsed milliseconds stay numeric.
            return {"started_at_ns": str(start) if start else None, "ended_at_ns": str(end) if end else None,
                    "elapsed_ms": max(0, ((end or now) - start) // 1_000_000) if start else None}
        state["subagents"] = [{"id": key, "tool_call_id": "kc_" + digest(key),
            "agent": item["action"]["arguments"]["agent"],
            "description": item["action"]["arguments"].get("description"),
            "task": item["action"]["arguments"]["task"],
            "status": child_result(key, item).get("status", "running"),
            "background": bool(item.get("outcome", {}).get("data", {}).get("background") or item["action"]["arguments"].get("background")),
            "summary": child_result(key, item).get("data", {}).get("summary", ""),
            "partial": child_result(key, item).get("data", {}).get("partial", False),
            "reason": child_result(key, item).get("data", {}).get("reason") or child_result(key, item).get("data", {}).get("completion_reason"),
            **child_times(key, item),
            **child_activity.get(key + "/leaf", {"steps": 0, "tool_calls": 0, "latest_tool": None,
                                                 "latest_detail": None, "tokens": 0})}
            for key, item in execution["actions"].items() if item["action"]["tool"] == "delegate"]
        if state["goal"]:
            from kernelcat_executor.state.goal_progress import goal_usage
            state["goal_usage"] = goal_usage(list(self.owner.gateway.store._events(self.owner.gateway.store.events())), state["goal"])
            goal = state["goal"]
            state["goal_usage"]["elapsed_seconds"] = max(0, (goal.get("ended_at_ns", time.time_ns()) - goal.get("started_at_ns", time.time_ns())) // 1_000_000_000)
        state["calls"] = {key: {field: item[field] for field in ("method", "status", "intent_seq") if field in item}
                          for key, item in state["calls"].items() if item["status"] == "pending"}
        state["checkpoints"] = {key: {field: item[field] for field in ("source_sha256", "entry_id", "pi_session_id") if field in item}
                                for key, item in state["checkpoints"].items()}
        from kernelcat_worker.loopx_application import recommend
        state["fork_suggestion"] = recommend(state).get("fork_hint")
        state["name"] = next((item["name"] for item in reversed(state["configurations"]) if item.get("operation") == "rename"), None)
        return state

    def trial_exhausted(self):
        state = self.status()
        start = state["trials"][-1].get("start_model_turns", 0) if state["trials"] else 0
        return state["max_trial_turns"] is not None and state["model_turns"] - start >= state["max_trial_turns"]

    def capture_source(self):
        if self.config.workspace_mode == "project":
            from kernelcat_executor.contracts.models import digest
            # An explicit marker, never a claim that all project files were captured.
            return {"source_sha256": digest({"snapshot_kind": "conversation_only"}),
                    "snapshot_kind": "conversation_only", "files": []}
        policy = self.owner.workspace_policy
        return source_snapshot(Path(policy["root"]), policy["writable_paths"], policy["readonly_paths"])

    def finish_codegen_goal(self, action):
        from kernelcat_executor.contracts.models import Outcome
        if self.config.goal_review.enabled:
            return Outcome(data={"completed": False, "review_pending": True,
                "summary": action.arguments["summary"], "device_acceptance": False})
        self.journal.command("goal_outcome", {"status": "completed", "completion_kind": "codegen",
            "action_id": action.id, "summary": action.arguments["summary"]})
        return Outcome(data={"completed": True, "summary": action.arguments["summary"], "device_acceptance": False})

    def get_codegen_goal(self, action):
        from kernelcat_executor.contracts.models import Outcome
        state = self.status()
        goal = {key: str(value) if key.endswith("_ns") else value for key, value in state["goal"].items()} if state["goal"] else None
        return Outcome(data={"goal": goal, "usage": state.get("goal_usage"), "budget": state["budget"]})

    def update_codegen_goal(self, action):
        from kernelcat_executor.contracts.models import Outcome
        if action.arguments["status"] == "complete":
            return self.finish_codegen_goal(action)
        goal = self.journal.state()["goal"]
        if not goal or goal["status"] != "active":
            raise Conflict("Only an active Goal can report a blocker")
        self.journal.command("goal_outcome", {"status": "awaiting_user", "reason": "blocked",
            "summary": action.arguments["summary"], "action_id": action.id})
        return Outcome(data={"status": "awaiting_user", "reason": "blocked", "summary": action.arguments["summary"]})

    def next_input(self, connection):
        from kernelcat_worker.loopx_application import recommend
        state = self.status()
        if state.get("takeover_requested"):
            return {"action": "wait", "reason": "takeover"}
        queued = [item for item in state["inputs"].values() if item["status"] == "queued" and item["kind"] != "control"]
        if queued:
            return {"action": queued[0].get("transition_operation", "input"), "receipt": queued[0]}
        if (self.config.goal_review.enabled and self.config.workspace_mode == "project"
                and state["goal"] and state["goal"]["status"] == "active"
                and not state["calls"] and not state["pending_effect_ids"]):
            latest = max((item for item in state["inputs"].values() if item["kind"] != "control"
                and item["status"] == "completed" and item["completed_seq"] > state["goal"]["start_seq"]),
                key=lambda item: item["completed_seq"], default=None)
            if latest:
                from kernelcat_worker.application_goal_review import review
                from kernelcat_executor.contracts.models import BudgetExceeded
                try:
                    review(self, connection, state, latest)
                except BudgetExceeded:
                    if self.journal.state()["goal"]["status"] == "active":
                        self.journal.command("goal_outcome", {"status": "budget_limit", "reason": "review_budget"})
                    return {"action": "wait", "reason": "review_budget"}
                except ModelAdmissionStopped:
                    return {"action": "wait", "reason": "review_admission_stopped"}
                state = self.status()
        decision = recommend(state)
        if decision["action"] == "stop":
            self.journal.command("goal_outcome", {"status": "budget_limit", "reason": decision["reason"]})
            return decision
        # A tool-free automatic continuation must not spin forever. A new user
        # request or explicit resume establishes a new continuation boundary.
        goal = state["goal"]
        settled = [item for item in state["inputs"].values() if item["kind"] != "control" and item["status"] == "completed"]
        latest = max(settled, key=lambda item: item["completed_seq"], default=None)
        if (self.config.workspace_mode == "project" and goal and goal["status"] == "active"
                and latest and latest["kind"] == "policy"
                and latest["completed_seq"] > max(goal.get("start_seq", -1), goal.get("resume_seq", -1))
                and not state["pending_effect_ids"] and not state["calls"]):
            from kernelcat_executor.state.goal_progress import continuation_has_progress
            if not continuation_has_progress(list(self.owner.gateway.store._events(self.owner.gateway.store.events())), latest):
                self.journal.command("goal_outcome", {"status": "awaiting_user", "reason": "no_tool_progress",
                    "summary": "Automatic continuation made no tool progress. Send new input or use /goal resume."})
                return {"action": "wait", "reason": "no_tool_progress"}
        if decision["action"] not in {"continue", "reset"}:
            return decision
        input_id = "loopx-" + decision["decision_id"]
        if input_id in state["inputs"]:
            return {"action": "wait", "reason": "decision_already_consumed"}
        self.journal.command("configuration", {**connection.worker, "operation": "loopx_decision", **decision})
        text = (("Continue the explicitly requested coding Goal. Implement and verify the remaining work. "
                "Use get_goal to inspect progress when needed. When all requested work is verified, call update_goal "
                "with status complete and a summary of actual checks (finish_goal is also supported). "
                "If progress requires missing user input, call update_goal with status blocked and explain what is needed. "
                "Do not call a missing device result verified. A tool-free automatic turn returns control to the user.\n"
                if self.config.workspace_mode == "project" else "Continue the current task from original receipts. "
                "Complete the declared correctness/performance acceptance; do not treat a single timing as a paired gain. "
                "Inspect missing or failed measurements before diagnosing stagnation.\n")
                + canonical({"task": state["task"], "goal": state["goal"], "budget": state["budget"],
                             "best": state["best"], "decision": decision}))
        receipt = self.journal.command("accept", {"input_id": input_id, "text": text, "kind": "policy",
            "initiator": "loopx", "channel": "policy", "decision_id": decision["decision_id"]})
        return {**decision, "receipt": receipt}

    def unsettled_foreground(self, state):
        """Pending work excluding running background children (read-only, own child tasks)."""
        background = {child["id"] for child in state["subagents"] if child.get("background") and child["status"] == "running"}
        effects = [key for key in state["pending_effect_ids"] if not any(key.startswith(agent + "/") for agent in background)]
        calls = [key for key, call in self.journal.state()["calls"].items() if call.get("status") == "pending"
                 and not (call.get("method") in {"child_model", "child_tool", "plugin_settle"}
                          and (call.get("params") or {}).get("permit") in background)]
        return effects, calls

    def snapshot(self, connection, params):
        state = self.status()
        effects, calls = self.unsettled_foreground(state)
        if effects or calls:
            raise Conflict("source transition requires a settled execution boundary")
        workspace = Path(self.owner.workspace_policy["root"])
        policy = self.owner.workspace_policy
        operation = params.get("operation", "checkpoint")
        snapshot = self.capture_source()
        directory = self.owner.gateway.store.path.parent / "checkpoints"
        if operation == "fork":
            selected = self.journal.state()["checkpoints"].get(params["pi_session_id"] + ":" + params["entry_id"])
            if selected is None:
                raise Conflict("selected entry has no source checkpoint; historical source cannot be inferred")
            snapshot = read_source_snapshot(directory / (selected["source_sha256"] + ".json"), selected["source_sha256"])
            params = {**params, "native_checkpoint": selected.get("native_checkpoint"), "session_file": selected.get("session_file")}
        persist(directory / (snapshot["source_sha256"] + ".json"), snapshot)
        checkpoint = {**connection.worker, "source_sha256": snapshot["source_sha256"],
            "snapshot_kind": snapshot.get("snapshot_kind", "source"),
            "pi_session_id": params["pi_session_id"], "entry_id": params["entry_id"],
            "session_file": params.get("session_file"), "operation": operation,
            "native_checkpoint": params.get("native_checkpoint")}
        if self.owner.rsi is not None:
            records = self.owner.rsi.records
            entries = records.entries()
            if entries:
                checkpoint["rsi_context"] = (selected.get("rsi_context") if operation == "fork" else None) or {
                    "baseline_record_id": records.baseline()["record_id"], "record_sequence": len(entries),
                    "branch_id": (records.latest("branch") or {}).get("record_id")}
        if checkpoint["native_checkpoint"]:
            self.owner.replay.checkpoint(checkpoint["native_checkpoint"])
        if operation in {"fork", "reset", "clone"}:
            goal = state["goal"]
            if self.config.workspace_mode == "project" and goal and goal["status"] == "active" and operation in {"fork", "clone"}:
                forks = sum(item.get("goal_id") == goal.get("goal_id") and item["parent"]["operation"] in {"fork", "clone"}
                            for item in state["context_transitions"])
                if goal.get("max_forks") is not None and forks >= goal["max_forks"]:
                    raise Conflict("Goal fork ceiling reached")
            limits = [value for value in ((goal or {}).get("max_trials"),
                goal["max_forks"] + 1 if goal and goal.get("max_forks") is not None else None) if value is not None]
            if self.config.workspace_mode != "project" and limits and len(state["trials"]) >= min(limits):
                raise Conflict("Goal Trial/fork ceiling reached")
            self.journal.command("configuration", {**connection.worker, "operation": "transition_prepared",
                "transition_id": str(uuid.uuid4()), "goal_id": (goal or {}).get("goal_id"), "parent": checkpoint})
            if operation == "fork" and self.config.workspace_mode != "project":
                restore_source(workspace, snapshot, policy["writable_paths"], policy["readonly_paths"])
        else:
            self.journal.command("checkpoint", checkpoint)
        return {**checkpoint, "handoff": {"goal": state["goal"], "task_version": state["task_version"],
            "source_sha256": snapshot["source_sha256"], "best_accepted": state["best"],
            "single_best": state["single_best"], "platform": state["platform"], "budget": state["budget"],
            "pending_effect_ids": state["pending_effect_ids"], "user_constraints": [item["text"] for item in state["inputs"].values()
                if item["initiator"] == "user" and item["kind"] in {"prompt", "steer", "follow_up"}]}}

    def select_session(self, connection, params):
        if not params.get("path"):
            return {"sessions": list({item["session_file"] for item in self.journal.state()["checkpoints"].values() if item.get("session_file")})}
        path = Path(params.get("path") or "")
        root = self.owner.gateway.store.path.parent / "pi-sessions"
        if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(root):
            raise Conflict("select an archived native session inside this application's pi-sessions directory")
        rows = [json.loads(line) for line in read_regular(path, 32 * 1024 * 1024).splitlines()]
        header = rows[0]
        if header.get("type") != "session" or header.get("cwd") != self.owner.workspace_policy["root"]:
            raise Conflict("imported session belongs to another workspace")
        known = self.journal.state()["checkpoints"].values()
        selected = next((item for item in known if item["pi_session_id"] == header.get("id") and item["entry_id"] == rows[-1].get("id")), None)
        if selected is None:
            raise Conflict("imported context has no matching source checkpoint")
        state = self.status()
        if state["pending_effect_ids"] or state["calls"]:
            raise Conflict("Resume requires a settled execution boundary")
        if self.config.workspace_mode != "project":
            snapshot = read_source_snapshot(self.owner.gateway.store.path.parent / "checkpoints" / (selected["source_sha256"] + ".json"), selected["source_sha256"])
            policy = self.owner.workspace_policy
            restore_source(Path(policy["root"]), snapshot, policy["writable_paths"], policy["readonly_paths"])
        self.journal.command("checkpoint", {**selected, **connection.worker, "operation": "checkpoint"})
        if selected.get("native_checkpoint"):
            self.owner.replay.checkpoint(selected["native_checkpoint"])
        return {"path": str(path), "checkpoint": selected}

    def configure(self, connection, params):
        state = self.status()
        kind, value = params.get("kind"), params.get("value")
        if kind == "model":
            allowed = list(self.config.policy.routes)
            if value is None:
                return {"allowed_routes": allowed, "current": self.owner.gateway.store.policy().routes[0]}
            if value not in allowed:
                raise Conflict("model route is outside the frozen profile")
            if state["pending_effect_ids"]:
                raise Conflict("change model configuration at a settled boundary")
            from kernelcat_executor.contracts.models import digest
            policy = self.owner.gateway.store.policy()
            selected = policy.model_copy(update={"routes": (value, *(route for route in allowed if route != value)), "revision": policy.revision + 1})
            self.owner.gateway.store.install(digest(policy), selected, {"initiator": "user", "kind": "model"})
        elif kind == "thinking":
            configured = {name: item.wire_options.model_dump(mode="json") for name, item in self.config.providers.items()}
            if value is not None:
                routes = [name for name in self.config.policy.routes if value in {configured[name].get("thinking"), configured[name].get("reasoning_effort")}]
                if not routes:
                    raise Conflict("thinking level has no allowed route in the frozen profile")
                self.configure(connection, {"kind": "model", "value": routes[0]})
                self.journal.command("configuration", {**connection.worker, "kind": "thinking", "value": value, "route": routes[0]})
            return {"providers": configured}
        else:
            raise Conflict("unknown configuration kind")
        self.journal.command("configuration", {**connection.worker, "kind": kind, "value": value})
        return {"kind": kind, "value": value}

    def submit_effect(self, connection, params):
        """Queue one effect; the executor (or a child-model thread) settles the future."""
        self.require_worker(connection)
        future = Future()
        self.effects.put((connection, params, future))
        return future

    def require_worker(self, connection):
        lease = self.journal.state()["lease"]
        if not connection.worker or lease != connection.worker:
            raise Conflict("this connection does not hold the active execution lease")

    def broadcast(self, event):
        with self.mutex:
            for client in tuple(self.clients):
                if client.subscribed:
                    client.emit(event)

    def request(self, connection, request):
        method, params = request.get("method"), request.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("request params must be an object")
        if method == "status":
            return self.status()
        if method == "subscribe":
            connection.subscribed = True
            cursor = params.get("after", -1)
            if type(cursor) is not int or cursor < -1:
                raise Conflict("event cursor must be an integer at least -1")
            rows = self.owner.gateway.store.events()
            if cursor >= len(rows):
                raise Conflict("event cursor exceeds the original Journal")
            page, size = [], 0
            for row in rows[cursor + 1:cursor + 129]:
                encoded_size = len(canonical(row).encode())
                if page and size + encoded_size > 12 * 1024 * 1024:
                    break
                if encoded_size > 12 * 1024 * 1024:
                    raise Conflict("original event exceeds the socket page bound; inspect its Journal file")
                size += encoded_size
                page.append(row)
            end = page[-1]["seq"] if page else cursor
            return {"events": page, "cursor": end, "next_cursor": end if end < len(rows) - 1 else None}
        if method == "accept":
            if params.get("initiator") != "user":
                self.require_worker(connection)
            state = self.journal.state()
            if (params.get("kind") == "prompt" and params.get("input_id") not in state["inputs"]
                    and any(item["status"] == "applied" for item in state["inputs"].values())):
                raise Conflict("session is busy; submit explicitly as steer or follow_up")
            receipt = self.journal.command("accept", params)
            self.broadcast({"type": "input", "receipt": receipt})
            return receipt
        if method == "control":
            self.journal.command("accept", {"input_id": params["input_id"], "text": canonical(params),
                "kind": "control", "initiator": "user", "channel": params.get("channel", "cli")})
            self.journal.command("control", params)
            result = self.status()
            self.broadcast({"type": "control", "operation": params["operation"], "state": result})
            if params["operation"] in {"resume", "set"} and not result["lease"]:
                self.spawn_headless()
            return result
        if method == "ticket":
            with self.mutex:
                if self.journal.state()["lease"]:
                    raise Conflict("active worker must reach a safe detach boundary before attach")
                ticket = secrets.token_hex(32)
                self.tickets[ticket] = {"worker_id": str(uuid.uuid4()), "mode": params.get("mode", "interactive"),
                                        "expires": time.monotonic() + 30}
                return {"ticket": ticket, "worker_id": self.tickets[ticket]["worker_id"]}
        if method == "takeover":
            with self.mutex:
                lease = self.journal.state()["lease"]
                if lease and lease["mode"] != "headless":
                    raise Conflict("another interactive worker owns this session")
                if lease:
                    self.journal.command("configuration", {**lease, "operation": "takeover"})
                    self.broadcast({"type": "takeover"})
            return {"status": "waiting" if lease else "ready"}
        if method == "claim":
            with self.mutex:
                ticket = self.tickets.pop(params.get("ticket"), None)
                if not ticket or ticket.pop("expires") < time.monotonic():
                    raise Conflict("missing, expired or consumed worker ticket")
                # A recovering lease may only query original receipts until its
                # recovery handshake succeeds; it cannot authorize new effects.
                ticket["recovering"] = True
                state = self.journal.command("acquire", ticket)
                connection.worker = state["lease"]
                connection.owns_worker = True
                self.journal.command("configuration", {**connection.worker, "operation": "worker_ready"})
                return {"lease": connection.worker, "state": self.status()}
        if method == "bind_worker":
            current = self.journal.state()["lease"]
            if current != params.get("lease"):
                raise Conflict("control connection lease mismatch")
            connection.worker = current
            return {"lease": current}
        if method in {"transition", "checkpoint", "configuration", "trial", "task_version"}:
            self.require_worker(connection)
            if method == "trial":
                execution = self.owner.gateway.store.snapshot()
                params = {**params, "start_model_turns": self.status()["model_turns"],
                    "start_step": sum(1 for item in execution["actions"].values() if item["action"]["tool"] == "__turn_end__")}
                parent = (self.journal.state().get("pending_transition") or {}).get("parent", {})
                if parent.get("rsi_context"):
                    params["rsi_context"] = parent["rsi_context"]
            result = self.journal.command(method, {**params, **connection.worker})
            return result if method == "transition" else self.status()
        if method in {"snapshot", "select_session", "configure", "next"}:
            self.require_worker(connection)
            future = Future()
            self.effects.put((connection, {"method": "application_admin", "params": {"operation": method, "request": params}}, future))
            return future.result()
        if method == "effect":
            return self.submit_effect(connection, params).result()
        if method == "event":
            self.require_worker(connection)
            self.broadcast({"type": "harness", "event": params})
            return {}
        if method == "detach":
            self.require_worker(connection)
            mode = params.get("on_detach")
            if mode is not None and mode not in {"pause", "continue"}:
                raise Conflict("invalid detach mode")
            with self.mutex:
                for client in self.clients:
                    if client.worker == connection.worker:
                        client.detach_mode = mode
            return {"on_detach": connection.detach_mode or (self.journal.state()["goal"] or {}).get("on_detach", "pause")}
        raise ValueError("unknown application request")

    def disconnected(self, connection):
        with self.mutex:
            self.clients.discard(connection)
        if not connection.owns_worker:
            return
        future = Future()
        self.effects.put((connection, {"method": "close_scope"}, future))
        try:
            future.result()  # Wait for original local writes/remote settlement before releasing.
            self.journal.command("release", {**connection.worker, "reason": "worker_disconnected",
                **({"on_detach": connection.detach_mode} if connection.detach_mode else {})})
            state = self.status()
            goal = state["goal"]
            continuing = goal and goal["status"] == "active" and (connection.detach_mode or goal["on_detach"]) == "continue"
            if continuing and not state["pending_effect_ids"] and not any(x["status"] == "pending" for x in state["calls"].values()):
                if connection.worker["mode"] == "interactive":
                    self.spawn_headless()
        finally:
            self.broadcast({"type": "state", "state": self.status()})

    def spawn_headless(self):
        with self.mutex:
            if self.journal.state()["lease"] or any(item["expires"] > time.monotonic() for item in self.tickets.values()):
                return
            ticket = self.request(None, {"method": "ticket", "params": {"mode": "headless"}})["ticket"]
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "TERM": "dumb", "PI_OFFLINE": "1"}
        with open(Path(self.descriptor["output"]) / "logs" / "worker.log", "ab", buffering=0) as log:
            subprocess.Popen([self.descriptor["node"], self.descriptor["worker_entry"],
                "--host", str(self.directory), "--ticket", ticket, "--headless"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment, start_new_session=True)

    def serve(self):
        # The original host lock is already held; only now can a stale socket go.
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                raise Conflict("host socket path is occupied by a non-socket")
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        listener.listen(16)
        if self.restarting:
            state = self.journal.state()
            if state["goal"] and state["goal"]["status"] == "active" and state["goal"]["on_detach"] == "continue":
                self.spawn_headless()
        try:
            while not self.stopping.is_set():
                connection, _ = listener.accept()
                if peer_uid(connection) != os.getuid():
                    connection.close()
                    continue
                client = Connection(self, connection)
                threading.Thread(target=client.run, daemon=True).start()
        finally:
            listener.close()
            self.stopping.set()
            self.effects.put(None)
            self.socket_path.unlink(missing_ok=True)
            os.close(self.lock)


class Connection:
    def __init__(self, host, connection):
        self.host, self.connection = host, connection
        self.worker = None
        self.owns_worker = False
        self.detach_mode = None
        self.subscribed = False
        self.outgoing = deque()
        self.observations = deque()
        self.observation_bytes = 0
        self.outgoing_ready = threading.Condition()
        self.closed = threading.Event()

    def emit(self, message):
        encoded = canonical(message).encode() + b"\n"
        # RPC replies and durable input/control notifications cannot be displaced
        # by token deltas. Their slow-reader failure remains explicit; no effect
        # or input is retried. Display observations are bounded independently.
        presentation = message.get("type") in {"model_delta", "harness", "state"} and "id" not in message
        with self.outgoing_ready:
            if self.closed.is_set():
                return
            if not presentation:
                if len(self.outgoing) >= 256:
                    self.close("reliable response queue exhausted")
                    return
                self.outgoing.append(encoded)
            else:
                if message.get("type") == "state":
                    retained = deque()
                    for previous, payload in self.observations:
                        if previous.get("type") == "state":
                            self.observation_bytes -= len(payload)
                        else:
                            retained.append((previous, payload))
                    self.observations = retained
                elif message.get("type") == "model_delta" and self.observations:
                    previous, payload = self.observations[-1]
                    delta, old_delta = message.get("delta", {}), previous.get("delta", {})
                    if (previous.get("type") == "model_delta"
                            and previous.get("generation_id") == message.get("generation_id")
                            and delta.get("type") in {"text_delta", "thinking_delta"}
                            and delta.get("type") == old_delta.get("type")
                            and isinstance(delta.get("delta"), str) and isinstance(old_delta.get("delta"), str)
                            and len(payload) + len(encoded) <= 256 * 1024):
                        message = {**message, "delta": {**delta, "delta": old_delta["delta"] + delta["delta"]}}
                        encoded = canonical(message).encode() + b"\n"
                        self.observations.pop()
                        self.observation_bytes -= len(payload)
                # These are transient display hints, not execution receipts.
                # The complete model result and authoritative state are still
                # returned on the worker's separate effect/control connections.
                limit = 4 * 1024 * 1024
                while self.observations and (len(self.observations) >= 256 or self.observation_bytes + len(encoded) > limit):
                    _, removed = self.observations.popleft()
                    self.observation_bytes -= len(removed)
                if len(encoded) <= limit:
                    self.observations.append((message, encoded))
                    self.observation_bytes += len(encoded)
            self.outgoing_ready.notify()

    def close(self, reason=None):
        if reason and not self.closed.is_set():
            print(f"application connection closed: {reason}", file=sys.stderr, flush=True)
        self.closed.set()
        with self.outgoing_ready:
            self.outgoing_ready.notify_all()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def writer(self):
        while not self.closed.is_set():
            with self.outgoing_ready:
                self.outgoing_ready.wait_for(lambda: self.closed.is_set() or self.outgoing or self.observations)
                if self.closed.is_set():
                    return
                if self.outgoing:
                    payload = self.outgoing.popleft()
                else:
                    _, payload = self.observations.popleft()
                    self.observation_bytes -= len(payload)
            try:
                self.connection.sendall(payload)
            except OSError as error:
                self.close(f"socket write failed: {error}")

    def run(self):
        threading.Thread(target=self.writer, daemon=True).start()
        try:
            with self.connection.makefile("rb") as stream:
                authenticated = False
                while not self.closed.is_set():
                    raw = stream.readline(MAX_FRAME + 2)
                    if not raw:
                        break
                    if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                        raise ValueError("invalid application frame")
                    request = json.loads(raw)
                    REQUEST_VALIDATOR.validate(request)
                    identity = request.get("id")
                    try:
                        if not authenticated:
                            if request.get("method") != "authenticate" or not secrets.compare_digest(str(request.get("params", {}).get("token", "")), self.host.token):
                                raise Conflict("invalid or expired session token")
                            authenticated = True
                            with self.host.mutex:
                                self.host.clients.add(self)
                            result = {"application_session_id": self.host.identity}
                        elif request.get("method") == "effect":
                            # Do not block this reader on a running effect: a
                            # second child's request must reach the executor
                            # while the first child's model call is in flight.
                            pending = self.host.submit_effect(self, request.get("params", {}))
                            pending.add_done_callback(lambda done, identity=identity: self.emit(
                                {"id": identity, "result": done.result()} if done.exception() is None else
                                {"id": identity, "error": {"name": type(done.exception()).__name__, "message": str(done.exception())}}))
                            continue
                        else:
                            result = self.host.request(self, request)
                        self.emit({"id": identity, "result": result})
                    except Exception as error:
                        failure = {"id": identity, "error": {"name": type(error).__name__, "message": str(error)}}
                        if not authenticated:
                            # No subscriptions exist before authentication. Send
                            # this bounded rejection before closing the socket.
                            self.connection.settimeout(1)
                            self.connection.sendall(canonical(failure).encode() + b"\n")
                            break
                        self.emit(failure)
        finally:
            self.close()
            self.connection.close()
            self.host.disconnected(self)


def main(argv=None, *, expected_modules=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--descriptor", type=Path, required=True)
    args = parser.parse_args(argv)
    ApplicationHost(json.loads(args.descriptor.read_text()), expected_modules=expected_modules).serve()
    return 0
