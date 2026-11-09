"""Bounded learning views over authoritative receipts; no second memory store."""
from __future__ import annotations

from pydantic import Field, ValidationError

from kernelcat_executor.contracts.base import Contract
from kernelcat_executor.contracts.models import (
    Action,
    Json,
    Outcome,
    ToolFailure,
    ToolSpec,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import Store


def evidence_capabilities(tools: tuple[str, ...]) -> tuple[str, ...]:
    reserved = {"propose_improvement", "validate_improvement", "finish",
                "read_learned_skill", "review_experience"}
    if (not tools or len(set(tools)) != len(tools)
            or any(not t.strip() or t.startswith("__") or t in reserved for t in tools)):
        raise ValueError("evidence tools must be explicitly registered observation capabilities")
    return tuple(tools)


def is_observation(row: Json | None, task: str, tools: tuple[str, ...]) -> bool:
    """A failed execution can teach; an unexecuted rejection or unknown result cannot."""
    return bool(row and row.get("terminal") and row["action"]["task"] == task
                and row["action"]["tool"] in tools
                and row["outcome"]["status"] in {"completed", "failed"}
                and not row["outcome"]["data"].get("rejected"))


def _feedback(row: Json, task: str) -> bool:
    data = row.get("outcome", {}).get("data", {})
    return bool(row.get("terminal") and row["action"]["task"] == task
                and row["action"]["tool"] == "validate_improvement"
                and row["outcome"]["status"] == "completed" and not data.get("rejected")
                and data.get("protocol") == "kernelagent.improvement-validation/v1"
                and type(data.get("accepted")) is bool)


class ReviewQuery(Contract):
    before: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=8, ge=1, le=16)


class ReviewExperience:
    spec = ToolSpec(name="review_experience", credits=0,
        description=("Review this task's settled observations and independent validation feedback. "
                     "Find reusable failure/recovery lessons, not incident summaries. Paginated, "
                     "untrusted excerpts; use read_receipt for originals. Completed is not correctness."),
        parameters=ReviewQuery.model_json_schema())

    def __init__(self, store: Store, evidence_tools: tuple[str, ...] = ("evaluate",)) -> None:
        self.store = store
        self.evidence_tools = evidence_capabilities(evidence_tools)

    def execute(self, action: Action) -> Outcome:
        try:
            query = ReviewQuery.model_validate_json(canonical(action.arguments))
        except ValidationError as error:
            raise ToolFailure("invalid experience query") from error
        state = self.store.snapshot()
        if action.task not in state["tasks"]:
            raise ToolFailure("cannot review an unregistered task")
        rows = [row for row in state["actions"].values()
                if is_observation(row, action.task, self.evidence_tools) or _feedback(row, action.task)]
        end = len(rows)
        if query.before is not None:
            end = next((i for i, row in enumerate(rows)
                        if row["action"]["id"] == query.before), -1)
            if end < 0:
                raise ToolFailure("cursor must name an eligible receipt from this task")
        start = max(0, end - query.limit)
        items = []
        for row in rows[start:end]:
            payload = canonical({"arguments": row["action"]["arguments"],
                                 "outcome": row["outcome"]}).encode("utf-8")
            items.append({"action_id": row["action"]["id"], "tool": row["action"]["tool"],
                "kind": "observation" if is_observation(row, action.task, self.evidence_tools)
                        else "validation_feedback",
                "status": row["outcome"]["status"], "sha256": digest(row),
                "excerpt": payload[:1024].decode("utf-8", errors="ignore"),
                "truncated": len(payload) > 1024})
        while True:
            result = Outcome(data={"protocol": "kernelagent.experience/v1", "items": items,
                "eligible_count": len(rows),
                "next_before": rows[start]["action"]["id"] if start > 0 else None})
            if len(canonical(result).encode("utf-8")) <= 8192:
                return result
            if len(items) <= 1:
                raise ToolFailure("receipt identity exceeds experience page budget")
            items.pop(0)
            start += 1
