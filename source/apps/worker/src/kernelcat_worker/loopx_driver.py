"""One admitted LoopX driver per original Task; no external call or new ledger."""
from __future__ import annotations

from typing import Literal

from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, Outcome, Task
from kernelcat_executor.operations.gateway import Gateway


def bind_driver(gateway: Gateway, task: Task, driver: Literal["segment", "cycle"]) -> None:
    """Original Journal admission atomically excludes competing driver identities.

    Historical bindings remain their authority, without rewriting old receipts.
    Snapshot inspection is O(E); the existing Journal owns admission serialization.
    """
    rows = [row for row in gateway.store.snapshot()["actions"].values()
            if row["action"]["task"] == task.id]
    existing = "__loopx_binding__" if driver == "segment" else "__loopx_turn_request__"
    conflicting = "__loopx_turn_request__" if driver == "segment" else "__loopx_binding__"
    if any(row["action"]["tool"] == conflicting for row in rows):
        raise Conflict("Task already belongs to the other original LoopX driver")
    identity = task.id + "/loopx/driver"
    if any(row["action"]["tool"] == existing for row in rows) and not any(
            row["action"]["id"] == identity for row in rows):
        return
    gateway.store.register_task(task.model_dump(mode="json"))
    action = Action(id=identity, task=task.id, tool="__loopx_driver__", arguments={"driver": driver})
    outcome = gateway.internal(action, 0, lambda: Outcome(data={"driver": driver}))
    if outcome.status != "completed":
        raise InDoubt("original LoopX driver admission requires reconciliation: " + identity)
