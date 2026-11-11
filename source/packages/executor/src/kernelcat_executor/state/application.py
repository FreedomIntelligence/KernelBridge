"""Product sessions are projections of the execution Journal, never a second ledger.

Control requests use a separate connection from effects. Every transition is
committed under the Journal's existing lock and fsync before acknowledgement.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from kernelcat_executor.contracts.models import Conflict, digest
from kernelcat_executor.state.journal import Journal

PREFIX = "application/"
TERMINAL_INPUT = {"completed", "interrupted", "failed"}


class ModelAdmissionStopped(Conflict):
    """A control transition rejected a model before admission or spending."""
    def __init__(self, status: str):
        self.status = status
        super().__init__(f"model admission stopped by application state: {status}")


def project(rows: list[dict], identity: str) -> dict:
    state: dict[str, Any] = {"application_session_id": identity, "inputs": {}, "lease_epoch": 0,
                             "lease": None, "goal": None, "trials": [], "calls": {},
                             "task_version": 1, "cursor": len(rows) - 1, "configurations": [],
                             "checkpoints": {}, "context_transitions": [],
                             "evidence": [], "best": None, "single_best": None, "platform": []}
    for row in Journal._events(rows):
        if row.get("application_session_id") != identity:
            continue
        kind = row["kind"].removeprefix(PREFIX)
        data = row.get("data", {})
        if kind == "created":
            state.update(data)
        elif kind == "input_accepted":
            state["inputs"][data["input_id"]] = {**data, "accepted_seq": row["seq"], "status": "queued"}
        elif kind == "input_transition":
            state["inputs"][data["input_id"]].update(data, **{data["status"] + "_seq": row["seq"]})
        elif kind == "lease_granted":
            state.update(lease=data, lease_epoch=data["lease_epoch"])
        elif kind == "lease_revoked":
            state["lease"] = None
        elif kind == "goal":
            state["goal"] = data
        elif kind == "task_version":
            state.update(task_version=data["task_version"], task=data["task"])
            state.update(best=None, single_best=None)
        elif kind == "trial":
            state["trials"].append(data)
            state["trial_id"] = data["trial_id"]
            state["pending_transition"] = None
        elif kind == "call_intent":
            state["calls"][data["call_id"]] = {**data, "status": "pending", "intent_seq": row["seq"]}
        elif kind == "call_result":
            state["calls"][data["call_id"]].update(data, status="completed", result_seq=row["seq"])
        elif kind == "call_reconciled":
            state["calls"][data["call_id"]].update(data, status="reconciled", result_seq=row["seq"])
        elif kind == "checkpoint":
            state["checkpoint"] = {**data, "checkpoint_seq": row["seq"]}
            if data.get("entry_id") and data.get("pi_session_id"):
                state["checkpoints"][data["pi_session_id"] + ":" + data["entry_id"]] = data
        elif kind == "configuration":
            state["configurations"].append(data)
            if data.get("operation") == "transition_prepared":
                state["pending_transition"] = data
            elif data.get("operation") == "transition_aborted":
                state["pending_transition"] = None
            elif data.get("operation") == "context_started":
                if state.get("pending_transition"):
                    state["context_transitions"].append({**state["pending_transition"], "pi_session_id": data["pi_session_id"]})
                state["pending_transition"] = None
            elif data.get("operation") == "takeover":
                state["takeover_requested"] = True
            elif data.get("operation") == "worker_ready":
                state["takeover_requested"] = False
        elif kind == "evidence":
            state["evidence"].append(data)
            if data["evidence_tier"] == "E4":
                state["platform"].append(data)
            elif data["evidence_tier"] == "E3":
                prior = state["best"]
                # A win over an older branch is not a comparison against the
                # current incumbent. Keep it as evidence until that pair exists.
                if prior is None or (data.get("parent_source_sha256") == prior["source_sha256"]
                    and data["measurement_profile_version"] == prior["measurement_profile_version"]):
                    state["best"] = data
            elif data["evidence_tier"] == "E2":
                prior = state["single_best"]
                if prior is None or data.get("score", float("-inf")) > prior.get("score", float("-inf")):
                    state["single_best"] = data
    return state


class ApplicationJournal:
    def __init__(self, journal: Journal, identity: str):
        if not isinstance(identity, str) or not identity or len(identity) > 128:
            raise ValueError("invalid application session identity")
        self.journal, self.identity = journal, identity

    def state(self) -> dict:
        return self.journal._transaction(lambda rows: (None, project(rows, self.identity)))

    def command(self, method: str, data: dict) -> dict:
        data = json.loads(json.dumps(data, allow_nan=False))
        def transaction(rows):
            state = project(rows, self.identity)
            events: list[dict] = []
            def emit(kind, value):
                events.append({"kind": PREFIX + kind, "application_session_id": self.identity,
                               "data": value, "time_ns": time.time_ns()})
            def lease():
                current = state["lease"]
                if (not current or current["lease_epoch"] != data.get("lease_epoch")
                        or current["worker_id"] != data.get("worker_id")):
                    raise Conflict("stale or missing execution lease")
            def stable():
                if any(call["status"] == "pending" for call in state["calls"].values()):
                    raise Conflict("unresolved calls require original receipt reconciliation")
            if method == "create":
                if "task" in state:
                    if state["creation_digest"] != digest(data):
                        raise Conflict("application identity reused with different configuration")
                    return None, state
                created = {**data, "creation_digest": digest(data)}
                if created.get("goal"):
                    created["goal"] = {**created["goal"], "start_seq": len(rows), "started_at_ns": time.time_ns()}
                emit("created", created)
            elif "task" not in state:
                raise Conflict("application session has not been created")
            elif method == "accept":
                key = data.get("input_id")
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise Conflict("input_id must contain 1..128 characters")
                if data.get("kind") not in {"prompt", "steer", "follow_up", "control", "policy"}:
                    raise Conflict("invalid input kind")
                if data.get("initiator") not in {"user", "agent", "loopx"} or data.get("channel") not in {"tui", "sdk", "cli", "policy"}:
                    raise Conflict("input provenance is required")
                if not isinstance(data.get("text"), str) or len(data["text"].encode()) > 262144:
                    raise Conflict("input must be bounded UTF-8 text")
                previous = state["inputs"].get(key)
                checksum = digest(data)
                if previous:
                    if previous["content_digest"] != checksum:
                        raise Conflict("input ID reused with different content")
                    return None, previous
                emit("input_accepted", {**data, "content_digest": checksum, "task_version": state["task_version"]})
                if (data["initiator"] == "user" and data["kind"] in {"prompt", "steer", "follow_up"}
                        and state["goal"] and state["goal"]["status"] == "completed"):
                    emit("goal", {**state["goal"], "status": "cleared", "reason": "new_user_request"})
            elif method == "transition":
                lease()
                previous = state["inputs"].get(data.get("input_id"))
                if not previous:
                    raise Conflict("input was not accepted")
                target = data.get("status")
                if target == previous["status"]:
                    return None, previous
                allowed = ({"completed", "failed", "interrupted"} if previous["kind"] == "control" else {"applied", "failed", "interrupted"}) if previous["status"] == "queued" else TERMINAL_INPUT
                if (previous["status"] in TERMINAL_INPUT or target not in allowed):
                    raise Conflict("invalid input transition")
                if target == "applied" and not (data.get("entry_id") and data.get("pi_session_id")):
                    raise Conflict("applied input requires its persisted Pi entry")
                emit("input_transition", {**data, "task_version": state["task_version"]})
                if target == "applied" and state["goal"] and state["goal"]["status"] == "awaiting_user" and previous["initiator"] == "user":
                    goal = {key: value for key, value in state["goal"].items() if key not in {"reason", "summary"}}
                    emit("goal", {**goal, "status": "active", "resume_seq": len(rows)})
            elif method == "acquire":
                if not data.get("recovering"):
                    stable()
                if state["lease"]:
                    raise Conflict("another worker owns the execution lease")
                if data.get("mode") not in {"interactive", "headless"} or not data.get("worker_id"):
                    raise Conflict("worker identity and mode required")
                emit("lease_granted", {**data, "lease_epoch": state["lease_epoch"] + 1})
            elif method == "release":
                lease()
                # Retained intents fence subsequent writers even after the old socket dies.
                emit("lease_revoked", data)
                goal = state["goal"]
                if goal and goal["status"] == "active" and data.get("on_detach", goal["on_detach"]) == "pause":
                    emit("goal", {**goal, "status": "paused"})
            elif method == "control":
                received = state["inputs"].get(data.get("input_id"))
                if not received or received["kind"] != "control":
                    raise Conflict("control command requires its durable input receipt")
                if received["status"] == "completed":
                    return None, state
                operation = data.get("operation")
                goal = state["goal"]
                if operation == "set":
                    if not isinstance(data.get("objective"), str) or not data["objective"].strip():
                        raise Conflict("goal objective required")
                    previous = goal or {}
                    goal = {**state.get("goal_defaults", {}),
                            **{key: previous[key] for key in ("on_detach", "max_trials", "max_model_turns", "max_forks", "fork_policy", "token_budget") if key in previous},
                            "objective": data["objective"], "status": "active", "goal_id": data["input_id"],
                            "start_seq": len(rows), "started_at_ns": time.time_ns(),
                            "on_detach": previous.get("on_detach", "pause"), "task_version": state["task_version"]}
                    if "token_budget" in data:
                        value = data["token_budget"]
                        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                            raise Conflict("Goal token budget must be a positive integer or null")
                        goal["token_budget"] = value
                    task = {**state["task"], "goal": data["objective"]}
                    if "acceptance" in data:
                        criteria = data["acceptance"]
                        if (not isinstance(criteria, dict) or set(criteria) - {"evidence_tier", "target"}
                                or criteria.get("evidence_tier") not in {"E0", "E1", "E2", "E3", "E4"}):
                            raise Conflict("acceptance update requires an evidence tier and optional numeric target")
                        target = criteria.get("target")
                        if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target)):
                            raise Conflict("acceptance target must be finite")
                        contract = task.get("contract", {})
                        task["contract"] = {**contract, "acceptance": {**contract.get("acceptance", {}), **criteria}}
                    if task != state["task"]:
                        version = state["task_version"] + 1
                        emit("task_version", {"task_version": version, "task": task})
                        goal["task_version"] = version
                elif not goal and operation in {"interrupt", "pause", "resume", "clear"}:
                    emit("configuration", {"operation": "session_" + operation, "input_id": data["input_id"]})
                elif not goal:
                    raise Conflict("session has no Goal")
                elif operation in {"pause", "resume", "interrupt"}:
                    # Terminal controls are idempotent after completion/clear.
                    # Starting work again requires an explicit new Goal.
                    if goal["status"] not in {"completed", "cleared"}:
                        goal = {**goal, "status": {"pause": "paused", "resume": "active", "interrupt": "awaiting_user"}[operation]}
                        if operation == "resume":
                            goal = {key: value for key, value in goal.items() if key != "reason"}
                            goal["resume_seq"] = len(rows)
                elif operation == "on-detach" and data.get("value") in {"pause", "continue"}:
                    goal = {**goal, "on_detach": data["value"]}
                elif operation == "clear":
                    goal = {**goal, "status": "cleared"}
                else:
                    raise Conflict("unknown Goal control")
                if goal:
                    emit("goal", goal)
                if operation in {"set", "clear", "pause", "interrupt"}:
                    for key, item in state["inputs"].items():
                        if item["kind"] == "policy" and item["status"] == "queued":
                            emit("input_transition", {"input_id": key, "status": "interrupted", "reason": "goal_" + operation})
                emit("input_transition", {"input_id": data["input_id"], "status": "completed", "control_result": goal})
            elif method == "goal_outcome":
                goal = state["goal"]
                if not goal or data.get("status") not in {"completed", "budget_limit", "awaiting_user", "failed"}:
                    raise Conflict("invalid Goal outcome")
                if data["status"] == "completed":
                    evidence = next((item for item in state["evidence"] if item["evidence_ref"] == data.get("evidence_ref")), None)
                    entry = self.journal._state(rows)["actions"].get(data.get("action_id"), {})
                    action = entry.get("action", {})
                    codegen = (state.get("workspace_mode") == "project" and goal["status"] == "active"
                               and entry.get("seq", -1) > goal.get("start_seq", -1)
                               and data.get("completion_kind") == "codegen" and action.get("tool") in {"finish_goal", "update_goal"}
                               and isinstance(data.get("summary"), str) and bool(data["summary"].strip()))
                    reviewed = False
                    if (state.get("workspace_mode") == "project" and goal["status"] == "active"
                            and data.get("completion_kind") == "review" and entry.get("terminal")
                            and entry.get("seq", -1) > goal.get("start_seq", -1)
                            and action.get("tool") == "__propose__"
                            and action.get("id", "").startswith(state["task"]["id"] + "/goal-review/" + goal["goal_id"] + "/")):
                        from kernelcat_executor.state.goal_progress import review_verdict
                        try:
                            reviewed = review_verdict(entry["outcome"]) == {"status": "met", "summary": data.get("summary")}
                        except (ValueError, KeyError):
                            pass
                    if not codegen and not reviewed and (not evidence or evidence["task_version"] != state["task_version"]):
                        raise Conflict("Goal completion requires current original acceptance evidence")
                emit("goal", {**goal, **data, **({"ended_at_ns": time.time_ns()} if data["status"] in {"completed", "failed"} else {})})
            elif method == "call_intent":
                lease()
                key = data["call_id"]
                previous = state["calls"].get(key)
                if previous:
                    if previous["request_digest"] != digest(data):
                        raise Conflict("call ID reused with different request")
                    return None, previous
                emit("call_intent", {**data, "request_digest": digest(data), "task_version": state["task_version"],
                    "trial_id": state.get("trial_id"), "goal_id": (state["goal"] or {}).get("goal_id")})
            elif method == "call_result":
                previous = state["calls"].get(data["call_id"])
                if not previous:
                    raise Conflict("result has no original intent")
                if previous["status"] == "completed":
                    if digest(previous["result"]) != digest(data["result"]):
                        raise Conflict("conflicting call result")
                    return None, previous
                emit("call_result", data)
            elif method == "call_reconciled":
                if data.get("call_id") not in state["calls"]:
                    raise Conflict("reconciled call has no original intent")
                emit("call_reconciled", data)
            elif method in {"checkpoint", "configuration"}:
                lease()
                emit(method, data)
            elif method == "trial":
                lease()
                stable()
                if not all(data.get(key) for key in ("trial_id", "source_sha256", "pi_session_id")):
                    raise Conflict("Trial requires frozen source and native session identities")
                if any(t["trial_id"] == data["trial_id"] for t in state["trials"]):
                    raise Conflict("Trial already exists")
                emit("trial", data)
            elif method == "task_version":
                lease()
                emit("task_version", {"task_version": state["task_version"] + 1, "task": data["task"]})
            elif method == "evidence":
                from kernelcat_executor.state.acceptance_evidence import validate_evidence
                previous = next((item for item in state["evidence"] if item["evidence_ref"] == data.get("evidence_ref")), None)
                if previous:
                    if digest(previous) != digest(data):
                        raise Conflict("conflicting acceptance receipt")
                    return None, state
                validate_evidence(data, state)
                emit("evidence", data)
            else:
                raise Conflict("unknown application command")
            event = events[0] if len(events) == 1 else {"kind": "atomic", "events": events}
            updated = project([*rows, {**event, "seq": len(rows)}], self.identity)
            result = updated["inputs"][data["input_id"]] if method in {"accept", "transition"} else updated
            return event, result
        return self.journal._transaction(transaction)
