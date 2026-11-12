"""Pi process handle with durable input and lifecycle ownership; no Python Agent loop."""
from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from functools import partial
from pathlib import Path
from threading import Event, Lock, RLock, get_ident
from uuid import uuid4

from kernelcat_executor.capabilities.pi import default_pi_root
from kernelcat_executor.contracts.agent import (
    AgentEvent,
    AgentInput,
    AgentOptions,
    AgentState,
    EventKind,
    Listener,
)
from kernelcat_executor.contracts.harness import NativeReplay
from kernelcat_executor.contracts.messages import AssistantTurn, ToolBatchPlan, turn_actions
from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    Policy,
    Task,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import ProfileStore, Proposer
from kernelcat_executor.contracts.profiles import AgentProfile
from kernelcat_executor.contracts.rsi import RsiSession
from kernelcat_executor.operations.batches import execute_parallel_calls, running_in_batch
from kernelcat_executor.operations.context import ContextOverflow, project
from kernelcat_executor.operations.control import SessionControl, restore_messages
from kernelcat_executor.operations.events import EventBus
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.hooks import OutcomeObserver
from kernelcat_executor.operations.recovery import ModelRetry, authorize_model_retry
from kernelcat_executor.operations.turns import BatchResult, CallCancelled, read_batch_plan
from kernelcat_executor.state.goal_progress import model_call_progress

RPCHandler = Callable[[str, Json], Json]
RPCTransport = Callable[[RPCHandler], Json]


class ExecutionSession:
    def __init__(self, gateway: Gateway, proposers: dict[str, Proposer],
                 skills: list[Json], *, profile: AgentProfile | None = None,
                 observers: tuple[OutcomeObserver, ...] = (),
                 cancelled: Callable[[], bool] | None = None,
                 task_identity: str | None = None,
                 options: AgentOptions | None = None,
                 transform_context: Callable[[Json], Json] | None = None,
                 should_stop_after_turn: Callable[[AgentState], bool] | None = None,
                 argv: tuple[str, ...] | None = None, cwd: Path | None = None,
                 timeout: float = 300.0, max_frame_bytes: int = 1048576,
                 role: str = "coder", child_proposers: dict[str, Proposer] | None = None,
                 external_context: bool = False, rsi: RsiSession | None = None,
                 replay: NativeReplay | None = None, workspace_policy: Json | None = None) -> None:
        if role not in {"coder", "scout"}:
            raise ValueError("unknown Pi worker role")
        node = os.environ.get("KERNELCAT_NODE") or shutil.which("node") or ""
        worker = Path(default_pi_root())
        self.worker_argv = argv if argv is not None else (node, str(worker / "src/harness/pi/worker.mjs"))
        self.worker_cwd = (cwd or Path.cwd()).resolve()
        self.workspace_policy = workspace_policy or {
            "root": str(self.worker_cwd), "writable_paths": None,
            "readonly_paths": ["reference"], "codemode": False}
        self.worker_timeout, self.worker_frame_bytes = timeout, max_frame_bytes
        self.role, self.child_proposers = role, dict(child_proposers or {})
        self.external_context = external_context
        self.rsi = rsi
        self.replay = replay
        self.gateway, self.proposers = gateway, dict(proposers)
        self.skills = [dict(s) for s in skills]
        self.profile, self.observers, self.cancelled = profile, tuple(observers), cancelled
        self.options = options or AgentOptions()
        self.transform_context = transform_context
        self.should_stop_after_turn = should_stop_after_turn
        self._lease, self._cancel, self._idle = Lock(), Event(), Event()
        self._idle.set()
        self._state_lock = RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._idle_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Future[bool]]] = set()
        self._closed, self._running = False, False
        self._task_identity = task_identity
        self._task: Task | None = None
        self._owner: int | None = None
        self._events = EventBus()
        self._control = SessionControl(gateway, self.options)
        self._messages: list[Json] = []
        self._pending: set[str] = set()
        self._error: str | None = None
        self._request_cancel: Event | None = None
        self._run_id, self._sequence = "", 0
        if profile is not None and not isinstance(gateway.store, ProfileStore):
            raise TypeError("versioned agent profiles require a ProfileStore")

    def _observe(self, action: Action, outcome: Outcome) -> None:
        for observer in self.observers:
            observer.after_action(self.gateway, Action.model_validate_json(canonical(action)),
                                  Outcome.model_validate_json(canonical(outcome)))

    @property
    def status(self) -> str:
        return "closed" if self._closed else "running" if self._running else "idle"

    @property
    def state(self) -> AgentState:
        with self._state_lock:
            return AgentState.model_validate_json(canonical({
                "status": self.status, "task_id": self._task.id if self._task else None,
                "messages": self._messages, "pending_tool_calls": sorted(self._pending),
                "error": self._error,
            }))

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        return self._events.subscribe(listener)

    def _emit(self, kind: EventKind, data: Json, step: int | None = None) -> None:
        assert self._task is not None
        event = AgentEvent(type=kind, task_id=self._task.id, run_id=self._run_id,
                           sequence=self._sequence, step=step, data=data)
        self._sequence += 1
        self._events.emit(event)

    def _message(self, message: Json, step: int, *, replayed: bool = False) -> None:
        self._emit("message_start", {"message": message, "replayed": replayed}, step)
        # Detach before subscribers see the end event, which is a dispatch barrier.
        import json
        with self._state_lock:
            self._messages.append(json.loads(canonical(message)))
        self._emit("message_end", {"message": message, "replayed": replayed}, step)

    def _require_task(self) -> Task:
        if self._closed:
            raise Conflict("agent is closed")
        if self._task is None:
            raise Conflict("run or prompt a task before controlling its session")
        return self._task

    def steer(self, text: str, *, message_id: str | None = None) -> str:
        return self._control.enqueue(self._require_task().id, text, "steer", message_id)

    def follow_up(self, text: str, *, message_id: str | None = None) -> str:
        return self._control.enqueue(self._require_task().id, text, "follow_up", message_id)

    def queued_messages(self) -> tuple[Json, ...]:
        return self._control.pending(self._require_task().id)

    def clear_steering_queue(self) -> tuple[Json, ...]:
        return self._control.clear(self._require_task().id, "steer")

    def clear_follow_up_queue(self) -> tuple[Json, ...]:
        return self._control.clear(self._require_task().id, "follow_up")

    def clear_all_queues(self) -> tuple[Json, ...]:
        return self._control.clear(self._require_task().id)

    def cancel(self) -> None:
        """Cooperative cancellation. Undetermined external effects remain reserved."""
        if self._running:
            self._cancel.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        if (self._events.delivering or running_in_batch(self)
                or (self._running and self._owner == get_ident())):
            raise Conflict("an active run cannot wait for its own settlement")
        return self._idle.wait(timeout)

    async def await_idle(self, timeout: float | None = None) -> bool:
        if (self._events.delivering or running_in_batch(self)
                or (self._running and self._owner == get_ident())):
            raise Conflict("an active subscriber cannot wait for its own settlement")
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._idle.is_set():
                return True
            waiter: asyncio.Future[bool] = loop.create_future()
            entry = (loop, waiter)
            self._idle_waiters.add(entry)
        try:
            return await asyncio.wait_for(waiter, timeout)
        except TimeoutError:
            return False
        finally:
            with self._state_lock:
                self._idle_waiters.discard(entry)

    @staticmethod
    def _resolve_idle(waiter: asyncio.Future[bool]) -> None:
        # Timeout or cancellation may win before the owner loop handles this callback.
        if not waiter.done():
            waiter.set_result(True)

    def close(self) -> None:
        with self._state_lock:
            if not self._lease.acquire(blocking=False):
                raise Conflict("cancel and drain the active run before closing its owner")
            try:
                self._closed = True
                executor, self._executor = self._executor, None
            finally:
                self._lease.release()
        if executor is not None:
            executor.shutdown(wait=True)

    def _checked_task(self, task: Task) -> Task:
        task = Task.model_validate_json(canonical(task))
        if task.scope != self.gateway.scope:
            raise Conflict("task scope differs from its runtime")
        if self._task_identity is not None and digest(task) != self._task_identity:
            raise Conflict("this runtime owns a different task workspace; compose a new agent")
        return task

    def _claim_run(self) -> None:
        with self._state_lock:
            if self._closed:
                raise Conflict("agent is closed")
            if not self._lease.acquire(blocking=False):
                raise Conflict("agent already owns an active run")
            self._idle.clear()
            self._cancel.clear()
            self._running = True

    def _release_run(self) -> None:
        with self._state_lock:
            self._pending.clear()
            self._running, self._owner = False, None
            self._request_cancel = None
            self._events.loop = None
            self._lease.release()
            self._idle.set()
            waiters = tuple(self._idle_waiters)
            self._idle_waiters.clear()
        for loop, waiter in waiters:
            try:
                loop.call_soon_threadsafe(self._resolve_idle, waiter)
            except RuntimeError:
                # A closed observer loop must not prevent owner teardown.
                pass

    def _cancelled(self) -> bool:
        return (self._cancel.is_set()
                or (self._request_cancel is not None and self._request_cancel.is_set())
                or (self.cancelled is not None and self.cancelled()))

    def run(self, task: Task, *, retry_model: str | None = None) -> Json:
        return self._drive(task, retry_model=retry_model)

    def serve(self, task: Task, transport: RPCTransport) -> Json:
        """Serve one externally driven finite RPC session without launching Pi."""
        return self._drive(task, transport=transport)

    def prompt(self, task: Task, text: str, *, message_id: str | None = None) -> Json:
        AgentInput(kind="prompt", text=text)
        return self._drive(task, text=text, message_id=message_id)

    def continue_run(self, task: Task | None = None) -> Json:
        return self.run(task if task is not None else self._require_task())

    async def arun(self, task: Task, *, retry_model: str | None = None) -> Json:
        return await self._async_drive(task, retry_model=retry_model)

    async def aprompt(self, task: Task, text: str, *, message_id: str | None = None) -> Json:
        AgentInput(kind="prompt", text=text)
        return await self._async_drive(task, text=text, message_id=message_id)

    async def _async_drive(self, task: Task, *, text: str | None = None,
                           message_id: str | None = None, retry_model: str | None = None) -> Json:
        loop = asyncio.get_running_loop()
        abort = Event()
        # Claim before enqueueing: a callback must reject a reentrant run,
        # never wait for work queued behind its own blocked owner.
        task = self._checked_task(task)
        call = partial(copy_context().run, self._drive, task, text=text,
                       message_id=message_id, event_loop=loop, request_cancel=abort,
                       claimed=True, retry_model=retry_model)
        with self._state_lock:
            self._claim_run()
            try:
                # Keep owner waits out of the callback/default executor. Reuse
                # one lazy worker per runtime; close() owns its final shutdown.
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=1,
                                                        thread_name_prefix="kernelcat-run")
                operation = loop.run_in_executor(self._executor, call)
            except BaseException:
                self._release_run()
                raise
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            abort.set()
            # Cancellation belongs to this invocation, not another active owner.
            # Repeated cancellation must not abandon a thread holding an effect.
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not operation.cancelled():
                operation.exception()
            raise

    def _drive(self, task: Task, *, text: str | None = None, message_id: str | None = None,
               event_loop: asyncio.AbstractEventLoop | None = None,
               request_cancel: Event | None = None, claimed: bool = False,
               retry_model: str | None = None,
               transport: RPCTransport | None = None) -> Json:
        if not claimed:
            task = self._checked_task(task)
            self._claim_run()
        try:
            if self._closed:
                raise Conflict("agent is closed")
            self._running, self._owner, self._task = True, get_ident(), task
            self._error, self._run_id, self._sequence = None, uuid4().hex, 0
            self._events.loop = event_loop
            self._request_cancel = request_cancel
            with self.gateway.store.task_lock(task.id):
                retry = (authorize_model_retry(self.gateway.store.snapshot(), task,
                         self.gateway.store.policy(), retry_model) if retry_model is not None else None)
                self.gateway.store.register_task(task.model_dump(mode="json"))
                restored = restore_messages(task, self.gateway.store.snapshot())
                with self._state_lock:
                    self._messages = restored
                if text is not None:
                    self._control.enqueue(task.id, text, "prompt", message_id)
                result: Json = {"status": "failed"}
                try:
                    self._emit("agent_start", {})
                    result = (self._run(task, retry=retry) if transport is None else
                              self._serve(task, transport, retry=retry))
                    return result
                except BaseException as error:
                    self._error = type(error).__name__
                    raise
                finally:
                    self._emit("agent_end", {"result": result, "error": self._error})
        finally:
            self._release_run()

    def _context(self, task: Task, state: Json, agent: Json | None) -> Json:
        import json
        policy = Policy.model_validate_json(canonical(state["policy"]))
        context = project(task, policy, state, self.skills, agent=agent,
            goal_progress=model_call_progress(self.gateway.store))
        if self.transform_context is None:
            return context
        transformed = self.transform_context(json.loads(canonical(context)))
        transformed = json.loads(canonical(transformed))
        if not isinstance(transformed, dict):
            raise Conflict("context transform must return an object")
        for key in ("task", "policy", "budget", "agent", "goal_progress"):
            if transformed.get(key) != context.get(key):
                raise Conflict(f"context transform changed protected {key}")
        if len(canonical(transformed).encode("utf-8")) > policy.context_bytes:
            raise ContextOverflow("transformed context exceeds context_bytes")
        return transformed

    def _dispatch(self, action: Action, step: int) -> Outcome:
        with self._state_lock:
            self._pending.add(action.id)
        try:
            self._emit("tool_execution_start", {"action": action.model_dump(mode="json")}, step)
            if self._cancelled():
                raise CallCancelled()
            outcome = self.gateway.execute(action)
            with self._state_lock:
                self._pending.discard(action.id)
            self._emit("tool_execution_end", {"action_id": action.id,
                       "outcome": outcome.model_dump(mode="json")}, step)
            if outcome.status != "unknown":
                self._message({"role": "toolResult", "action_id": action.id,
                               "tool_name": action.tool, "status": outcome.status,
                               "data": outcome.data}, step)
            return outcome
        finally:
            with self._state_lock:
                self._pending.discard(action.id)

    def _batch_plan(self, turn: AssistantTurn, task: Task, proposal_id: str,
                    state: Json) -> ToolBatchPlan | None:
        recorded = read_batch_plan(task.id, proposal_id, state)
        if recorded is not None:
            return recorded
        if self.options.tool_execution != "parallel" or len(turn.tool_calls) < 2:
            return None
        calls = turn_actions(turn, task.id, proposal_id)
        # A pre-existing serial attempt keeps its original dependency chain,
        # including requests rejected or interrupted before tool admission.
        if any(a.id in state["actions"] or a.id in state.get("requests", {}) for a in calls):
            return None
        if not self.gateway.parallel_safe(calls):
            return None
        plan = ToolBatchPlan(max_workers=self.options.max_parallel_tools)
        data = plan.model_dump(mode="json")
        outcome = self.gateway.internal(Action(id=proposal_id + "/batch", task=task.id,
            tool="__tool_batch__", arguments=data, parents=(proposal_id,)), 0,
            lambda: Outcome(data=data))
        if outcome.status != "completed":
            raise InDoubt(proposal_id + "/batch")
        return ToolBatchPlan.model_validate_json(canonical(outcome.data))

    def _delegation_plan(self, turn: AssistantTurn, task: Task, proposal_id: str,
                         state: Json) -> ToolBatchPlan | None:
        """Record independent delegate calls so their children may overlap.

        Only hosts that run child model calls concurrently opt in. A reply that
        mixes delegate with any other tool keeps its source-order chain.
        """
        if not getattr(self, "concurrent_delegation", False) or len(turn.tool_calls) < 2:
            return None
        if any(call.name != "delegate" for call in turn.tool_calls):
            return None
        calls = turn_actions(turn, task.id, proposal_id)
        if any(a.id in state["actions"] or a.id in state.get("requests", {}) for a in calls):
            return None
        plan = ToolBatchPlan(execution_mode="delegation", max_workers=min(32, len(calls)))
        data = plan.model_dump(mode="json")
        outcome = self.gateway.internal(Action(id=proposal_id + "/batch", task=task.id,
            tool="__tool_batch__", arguments=data, parents=(proposal_id,)), 0,
            lambda: Outcome(data=data))
        if outcome.status != "completed":
            raise InDoubt(proposal_id + "/batch")
        return ToolBatchPlan.model_validate_json(canonical(outcome.data))

    def _parallel_dispatch(self, calls: tuple[Action, ...], step: int,
                           plan: ToolBatchPlan) -> BatchResult:
        if not self.gateway.parallel_safe(calls):
            raise Conflict("recorded parallel batch no longer has concurrent tools")

        def started(action: Action) -> None:
            with self._state_lock:
                self._pending.add(action.id)
            self._emit("tool_execution_start", {"action": action.model_dump(mode="json")}, step)

        def settled(action: Action, outcome: Outcome) -> None:
            with self._state_lock:
                self._pending.discard(action.id)
            self._emit("tool_execution_end", {"action_id": action.id,
                       "outcome": outcome.model_dump(mode="json")}, step)

        def recorded(action: Action, outcome: Outcome) -> None:
            self._message({"role": "toolResult", "action_id": action.id,
                           "tool_name": action.tool, "status": outcome.status,
                           "data": outcome.data}, step)

        try:
            return execute_parallel_calls(calls, self.gateway.prepare, self._observe,
                self._cancelled, max_workers=plan.max_workers, owner=self, started=started,
                settled=settled, recorded=recorded)
        finally:
            # The executor drains before unwinding. Unstarted preparations have
            # no reservations, and cannot remain pending in the agent_end state.
            with self._state_lock:
                self._pending.difference_update(a.id for a in calls)

    @contextmanager
    def open_rpc(self, task: Task) -> Iterator[RPCHandler]:
        """Own one finite RPC scope after a managed bootstrap on an existing stream.

        This handle neither launches Pi nor chooses a next request. The caller must
        close the scope on EOF so native/delegated permits retain UNKNOWN outcomes.
        """
        from kernelcat_executor.operations.authority import ExecutionRPC

        task = self._checked_task(task)
        self._claim_run()
        authority = None
        result: Json = {"status": "interrupted"}
        try:
            self._running, self._owner, self._task = True, get_ident(), task
            self._error, self._run_id, self._sequence = None, uuid4().hex, 0
            with self.gateway.store.task_lock(task.id):
                self.gateway.store.register_task(task.model_dump(mode="json"))
                self._messages = restore_messages(task, self.gateway.store.snapshot())
                self._emit("agent_start", {})
                authority = ExecutionRPC(self, task)
                yield authority.handle
                result = authority.result or result
        except BaseException as error:
            self._error = type(error).__name__
            raise
        finally:
            try:
                if authority is not None:
                    authority.close()
                    self._emit("agent_end", {"result": result, "error": self._error})
            finally:
                self._release_run()

    def enqueue_input(self, task: Task, text: str, kind: str, input_id: str) -> str:
        """Trusted host ingress into the existing task queue, with durable identity."""
        self.gateway.store.register_task(task.model_dump(mode="json"))
        return self._control.enqueue(task.id, text, kind, input_id)

    def _serve(self, task: Task, transport: RPCTransport, *,
               retry: ModelRetry | None = None) -> Json:
        from kernelcat_executor.capabilities.worker import WorkerError
        from kernelcat_executor.operations.authority import ExecutionRPC
        authority = ExecutionRPC(self, task, retry=retry)
        try:
            return transport(authority.handle)
        except WorkerError:
            if self._cancelled():
                return {"status": "cancelled", "step": authority.step}
            raise
        finally:
            authority.close()

    def _run(self, task: Task, *, retry: ModelRetry | None = None) -> Json:
        # The legacy embedded API still delegates conversational scheduling to Pi.
        from kernelcat_executor.capabilities.worker import run_worker
        return self._serve(task, lambda handle: run_worker(
            self.worker_argv, self.worker_cwd, handle, timeout=self.worker_timeout,
            max_frame_bytes=self.worker_frame_bytes, cancelled=self._cancelled), retry=retry)
