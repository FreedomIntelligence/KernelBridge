"""Read-only, task-scoped evidence retrieval exposed through the same gateway."""
from __future__ import annotations

from kernelcat_executor.contracts.models import (
    Action,
    Outcome,
    ToolFailure,
    ToolSpec,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import Store
from kernelcat_executor.contracts.text import text_page


class ReceiptReader:
    spec = ToolSpec(name="read_receipt", description=(
        "Read one completed receipt from this task, using an action_id and optional page cursor. "
        "offset, limit and next_offset count Unicode code points, not bytes or lines. "
        "Follow returned next_offset; null means end of receipt. "
        "Retrieve omitted context from evidence, never guess its contents."), credits=1,
        parameters={"type": "object", "properties": {
            "action_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0,
                       "description": "Zero-based Unicode code-point offset; use returned next_offset to continue."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8192,
                      "description": "Maximum Unicode code points requested. A bounded page may contain fewer; follow next_offset."}},
            "required": ["action_id"], "additionalProperties": False})

    def __init__(self, store: Store) -> None:
        self.store = store

    def execute(self, action: Action) -> Outcome:
        item = self.store.snapshot()["actions"].get(action.arguments["action_id"])
        if not item or item["action"]["task"] != action.task or not item.get("terminal"):
            raise ToolFailure("receipt unavailable for this task")
        if item["action"]["tool"].startswith("__rsi_"):
            raise ToolFailure("RSI evaluator receipts are private to the Controller")
        try:
            page = text_page(canonical(item), action.arguments.get("offset", 0),
                             action.arguments.get("limit", 2048))
        except ValueError as error:
            raise ToolFailure(str(error)) from None
        return Outcome(data={"action_id": item["action"]["id"], "receipt_digest": digest(item), **page})
