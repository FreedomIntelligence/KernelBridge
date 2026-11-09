"""Task-scoped input queues and turn checkpoints in the existing execution journal."""
from __future__ import annotations

from threading import RLock
from uuid import uuid4

from kernelcat_executor.contracts.agent import AgentInput, AgentOptions, InputKind
from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    Task,
    digest,
)
from kernelcat_executor.generated.runtime_control import ExecutionStamp
from kernelcat_executor.operations.gateway import Gateway


def input_identity(task_id: str, key: str) -> str:
    """Caller intent identifies one immutable input in the original task queue."""
    return f"{task_id}/input/{digest(key)}"


class SessionControl:
    """One consumer per task, protected by ExecutionSession's existing task lease.

    The lock coordinates this owner's queue producers and consumer. This is not
    a cross-machine mailbox. Journal admission still arbitrates every write.
    """
    def __init__(self, gateway: Gateway, options: AgentOptions) -> None:
        self.gateway, self.options = gateway, options
        self._lock = RLock()

    def _record(self, action: Action, data: Json) -> Outcome:
        result = self.gateway.internal(action, 0, lambda: Outcome(data=data))
        if result.status != "completed":
            raise InDoubt(action.id)
        return result

    def enqueue(self, task_id: str, text: str, kind: InputKind, key: str | None = None,
                *, expected: ExecutionStamp | None = None) -> str:
        message = AgentInput(kind=kind, text=text)
        identity = input_identity(task_id, key) if key is not None else f"{task_id}/input/{uuid4().hex}"
        arguments = message.model_dump(mode="json")
        if expected is not None:
            if key is None:
                raise Conflict("managed input requires a stable caller intent")
            arguments["expected"] = expected
        action = Action(id=identity, task=task_id, tool="__input__", arguments=arguments)
        with self._lock:
            self._record(action, message.model_dump(mode="json"))
        return action.id

    def _pending(self, task_id: str, state: Json | None = None) -> list[Json]:
        state = self.gateway.store.snapshot() if state is None else state
        records = [r for r in state["actions"].values() if r["action"]["task"] == task_id]
        consumed: set[str] = set()
        for record in records:
            if record["action"]["tool"] in {"__input_batch__", "__input_clear__"}:
                if not record.get("terminal"):
                    raise InDoubt(record["action"]["id"])
                consumed.update(record["outcome"]["data"]["input_ids"])
        pending = []
        for record in records:
            action = record["action"]
            if action["tool"] == "__input__" and action["id"] not in consumed:
                if not record.get("terminal"):
                    raise InDoubt(action["id"])
                if record["outcome"]["status"] != "completed":
                    continue
                pending.append({"id": action["id"], **record["outcome"]["data"]})
        return pending

    def pending(self, task_id: str) -> tuple[Json, ...]:
        with self._lock:
            return tuple(self._pending(task_id))

    def take(self, task_id: str, step: int, *, terminal: bool) -> tuple[Json, ...]:
        identity = f"{task_id}/step/{step}/inputs"
        with self._lock:
            state = self.gateway.store.snapshot()
            old = state["actions"].get(identity)
            if old is not None:
                if not old.get("terminal"):
                    raise InDoubt(identity)
                return tuple(old["outcome"]["data"]["messages"])
            pending = self._pending(task_id, state)
            prefix = f"{task_id}/step/{step}/model/"
            if any(r["action"]["id"].startswith(prefix) and r["action"]["tool"] == "__propose__"
                   for r in state["actions"].values()):
                # A request from an older runtime already fixed this turn's input.
                # New steering belongs to the next turn, never to a cached reply.
                pending = []
            # Explicit prompts and steering take precedence over follow-up work.
            prompts = [m for m in pending if m["kind"] == "prompt"]
            steering = [m for m in pending if m["kind"] == "steer"]
            following = [m for m in pending if m["kind"] == "follow_up"]
            if self.options.steering_mode == "one-at-a-time":
                steering = steering[:1]
            selected = prompts + steering
            if not selected and terminal:
                selected = following if self.options.follow_up_mode == "all" else following[:1]
            if not selected:
                return ()
            ids = [m["id"] for m in selected]
            data = {"input_ids": ids, "messages": selected, "step": step}
            action = Action(id=identity, task=task_id, tool="__input_batch__",
                            arguments=data, parents=tuple(ids))
            self._record(action, data)
            return tuple(selected)

    def clear(self, task_id: str, kind: InputKind | None = None) -> tuple[Json, ...]:
        with self._lock:
            selected = [m for m in self._pending(task_id) if kind is None or m["kind"] == kind]
            if selected:
                ids = [m["id"] for m in selected]
                action = Action(id=f"{task_id}/input-clear/{uuid4().hex}", task=task_id,
                                tool="__input_clear__", arguments={"input_ids": ids},
                                parents=tuple(ids))
                self._record(action, {"input_ids": ids})
            return tuple(selected)

    def checkpoint(self, task_id: str, step: int, proposal_id: str, prior: str,
                   result: Json | None, *, parents: tuple[str, ...] | None = None,
                   native_checkpoint: Json | None = None) -> None:
        identity = f"{task_id}/step/{step}/end"
        data = {"step": step, "proposal_id": proposal_id,
                "prior": identity if parents is not None else prior, "result": result}
        if native_checkpoint is not None:
            data["native_checkpoint"] = native_checkpoint
        self._record(Action(id=identity, task=task_id, tool="__turn_end__",
                            arguments=data, parents=(prior,) if parents is None else parents), data)

    def latest(self, task_id: str) -> Json | None:
        ends: list[Json] = []
        for record in self.gateway.store.snapshot()["actions"].values():
            action = record["action"]
            if action["task"] == task_id and action["tool"] == "__turn_end__":
                if not record.get("terminal"):
                    raise InDoubt(action["id"])
                ends.append(record["outcome"]["data"])
        if not ends:
            return None
        ends.sort(key=lambda end: end["step"])
        if len({end["step"] for end in ends}) != len(ends):
            raise Conflict("duplicate turn checkpoints")
        return ends[-1]

    def native_checkpoint_required(self, task_id: str) -> bool:
        # The requirement is part of the original model intent, before provider
        # dispatch, so the first process crash cannot erase negotiated replay.
        return any(item["action"]["task"] == task_id
                   and item["action"]["tool"] == "__propose__"
                   and item["action"]["arguments"].get("native_checkpoint_required") is True
                   for item in self.gateway.store.snapshot()["actions"].values())


def input_batches(task_id: str, state: Json) -> list[Json]:
    """Only activated inputs enter model context. Pending inputs are not instructions yet."""
    batches: list[Json] = []
    for record in state["actions"].values():
        action = record["action"]
        if action["task"] == task_id and action["tool"] == "__input_batch__":
            if not record.get("terminal"):
                raise InDoubt(action["id"])
            batches.append(record["outcome"]["data"])
    return sorted(batches, key=lambda batch: batch["step"])


def restore_messages(task: Task, state: Json) -> list[Json]:
    """Rebuild completed conversation turns; an unfinished turn will emit on resume."""
    from kernelcat_executor.contracts.messages import TURN_SCHEMA, AssistantTurn
    from kernelcat_executor.contracts.models import canonical
    from kernelcat_executor.operations.turns import planned_actions

    records = [r for r in state["actions"].values() if r["action"]["task"] == task.id]
    ends = [r["outcome"]["data"] for r in records
            if r["action"]["tool"] == "__turn_end__" and r.get("terminal")]
    finished = {end["proposal_id"]: end for end in ends}
    steps = {end["step"] for end in ends}
    messages: list[Json] = [{"role": "user", "text": task.goal, "source": "task"}]
    for record in records:
        action = record["action"]
        if not record.get("terminal"):
            continue
        data = record["outcome"]["data"]
        if action["tool"] == "__input_batch__" and data["step"] in steps:
            messages.extend({"role": "user", "text": m["text"], "input_id": m["id"],
                             "kind": m["kind"]} for m in data["messages"])
        if action["tool"] != "__propose__" or action["id"] not in finished:
            continue
        messages.append({"role": "assistant", "proposal_id": action["id"], **data})
        if data.get("schema_version") == TURN_SCHEMA:
            calls = planned_actions(AssistantTurn.model_validate_json(canonical(data)),
                                    task.id, action["id"], state)
            ids = [call.id for call in calls]
        else:
            # The checkpoint owns this association. Retry IDs are opaque and
            # route names may contain '/model/'; neither is a path grammar.
            ids = [finished[action["id"]]["prior"]]
        for identity in ids:
            result = state["actions"].get(identity)
            if result is None or not result.get("terminal"):
                raise InDoubt(identity)
            messages.append({"role": "toolResult", "action_id": identity,
                             "tool_name": result["action"]["tool"],
                             "status": result["outcome"]["status"],
                             "data": result["outcome"]["data"]})
    return messages
