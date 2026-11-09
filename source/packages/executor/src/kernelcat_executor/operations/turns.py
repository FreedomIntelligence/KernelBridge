"""Turn execution and transcript projection, not a second model/tool loop."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from kernelcat_executor.contracts.messages import (
    TURN_SCHEMA,
    AssistantTurn,
    ToolBatchPlan,
    turn_actions,
)
from kernelcat_executor.contracts.models import (
    Action,
    BudgetExceeded,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    canonical,
    digest,
)

BatchStatus = Literal["completed", "cancelled", "budget_exhausted", "budget_exhausted_during_update"]


class CallCancelled(Exception):
    """The owner cancelled after announcing a call but before dispatching it."""


@dataclass(frozen=True)
class BatchResult:
    status: BatchStatus
    outcomes: tuple[Outcome, ...]
    terminate: bool = False


def execute_calls(
    actions: tuple[Action, ...],
    execute: Callable[[Action], Outcome],
    observe: Callable[[Action, Outcome], None],
    cancelled: Callable[[], bool],
) -> BatchResult:
    """Execute through the caller's Gateway, one separately authorized call at a time.

    Replay/deduplication belongs to Gateway. Observer delivery may repeat after
    resume and keeps the existing receipt-identity idempotency requirement.
    """
    results: list[Outcome] = []
    for action in actions:
        if cancelled():
            return BatchResult("cancelled", tuple(results))
        try:
            outcome = execute(Action.model_validate_json(canonical(action)))
        except CallCancelled:
            return BatchResult("cancelled", tuple(results))
        except BudgetExceeded:
            return BatchResult("budget_exhausted", tuple(results))
        outcome = Outcome.model_validate_json(canonical(outcome))
        if outcome.status == "unknown":
            raise InDoubt(action.id)
        if outcome.data.get("rejected") and outcome.data.get("reason") == "budget":
            return BatchResult("budget_exhausted", tuple(results))
        results.append(outcome)
        try:
            observe(Action.model_validate_json(canonical(action)),
                    Outcome.model_validate_json(canonical(outcome)))
        except BudgetExceeded:
            return BatchResult("budget_exhausted_during_update", tuple(results))
    return BatchResult("completed", tuple(results),
                       bool(results) and all(o.data.get("terminate") is True for o in results))


def read_batch_plan(task_id: str, proposal_id: str, state: Json) -> ToolBatchPlan | None:
    record = state["actions"].get(proposal_id + "/batch")
    if record is None:
        return None
    if not record.get("terminal") or record.get("outcome", {}).get("status") != "completed":
        raise InDoubt(proposal_id + "/batch")
    plan = ToolBatchPlan.model_validate_json(canonical(record["outcome"]["data"]))
    expected = Action(id=proposal_id + "/batch", task=task_id, tool="__tool_batch__",
                      arguments=plan.model_dump(mode="json"), parents=(proposal_id,))
    if canonical(record["action"]) != canonical(expected):
        raise Conflict("batch plan differs from its durable request")
    return plan


def planned_actions(turn: AssistantTurn, task_id: str, proposal_id: str,
                    state: Json) -> tuple[Action, ...]:
    return turn_actions(turn, task_id, proposal_id,
                        parallel=read_batch_plan(task_id, proposal_id, state) is not None)


def native_proposals(task_id: str, state: Json) -> list[Json]:
    return [item for item in state["actions"].values()
            if item["action"]["task"] == task_id and item["action"]["tool"] == "__propose__"
            and item.get("outcome", {}).get("status") == "completed"
            and item["outcome"].get("data", {}).get("schema_version") == TURN_SCHEMA]


def _turn_results(turn: AssistantTurn, calls: tuple[Action, ...], state: Json,
                  preview: Callable[[object, int], object], limit: int, *,
                  checkpointed: bool) -> list[Json] | None:
    results: list[Json] = []
    missing = False
    for model_call, action in zip(turn.tool_calls, calls, strict=True):
        record = state["actions"].get(action.id)
        if record is None:
            if checkpointed:
                raise Conflict("completed native turn has no tool receipt: " + action.id)
            missing = True
            continue
        if not record.get("terminal"):
            raise InDoubt(action.id)
        if canonical(record["action"]) != canonical(action):
            raise Conflict("tool result differs from its assistant turn")
        result = Outcome.model_validate_json(canonical(record["outcome"]))
        if result.status == "unknown":
            raise InDoubt(action.id)
        results.append({"tool_call_id": model_call.id, "tool_name": model_call.name,
            "action_id": action.id, "receipt_sha256": digest(record),
            "status": result.status, "data": preview(result.data, limit), "read_tool": "read_receipt"})
    # A completed model can precede any tool intent after a lost ACK. Its sealed
    # calls resume from the original model receipt; no partial exchange enters a new input.
    return None if missing else results


def project_turns(
    task_id: str, state: Json, preview: Callable[[object, int], object], limit: int,
    *, max_items: int | None = None,
) -> list[Json]:
    """Project complete native turns only, retaining source-order tool results.

    Uncheckpointed calls without an admission are omitted as a whole exchange.
    Existing unresolved/unknown effects still stop projection. Legacy replies
    remain in the existing history projection.
    """
    groups: list[Json] = []
    proposals = native_proposals(task_id, state)
    if max_items is not None:
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        proposals = proposals[-max_items:]
    steps = {item["outcome"]["data"]["proposal_id"]: item["outcome"]["data"]["step"]
             for item in state["actions"].values()
             if item["action"]["task"] == task_id and item["action"]["tool"] == "__turn_end__"
             and item.get("terminal") and item["outcome"]["status"] == "completed"}
    for item in proposals:
        proposal = item["action"]
        data = item["outcome"]["data"]
        if not item.get("terminal"):
            raise InDoubt(proposal["id"])
        turn = AssistantTurn.model_validate_json(canonical(data))
        calls = planned_actions(turn, task_id, proposal["id"], state)
        results = _turn_results(turn, calls, state, preview, limit, checkpointed=proposal["id"] in steps)
        if results is None:
            continue
        group: Json = {"proposal_id": proposal["id"], "assistant": turn.model_dump(mode="json"),
                 "results": results}
        if proposal["id"] in steps:
            # Opaque retry identities do not encode their conversational position.
            group["step"] = steps[proposal["id"]]
        else:
            # Preserve projections of pre-checkpoint receipts.
            prefix, separator, suffix = proposal["id"].rpartition("/step/")
            if separator and prefix == task_id:
                group["step"] = int(suffix.split("/", 1)[0])
        groups.append(group)
    return groups
