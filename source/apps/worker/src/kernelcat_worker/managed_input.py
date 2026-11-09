"""Finite managed input admission through the bound session's original queue."""
from __future__ import annotations

from typing import cast

from kernelcat_executor.contracts.models import Conflict, Json
from kernelcat_executor.contracts.runtime import validate_runtime
from kernelcat_executor.generated.runtime import ManagedInputReceipt, ManagedInputRequest
from kernelcat_executor.operations.control import SessionControl, input_identity
from kernelcat_executor.state.runtime_store import RuntimeStore

from kernelcat_worker.managed_knowledge import ManagedKnowledge


def enqueue_managed_input(params: Json, *, task_id: str, control: SessionControl,
                          store: RuntimeStore, knowledge: ManagedKnowledge) -> ManagedInputReceipt:
    request = cast(ManagedInputRequest, validate_runtime(params, "ManagedInputRequest"))
    expected = request["expected"]
    actor = store.actor
    if expected["run_id"] != actor["run_id"] or expected["trial_id"] != actor["trial_id"]:
        raise Conflict("managed input does not address this connection's bound Run/Trial")
    identity = input_identity(task_id, request["intent_id"])
    # Only an existing operation may avoid a fresh Knowledge preflight. The
    # original Journal still verifies the Actor/body and rejects UNKNOWN replay.
    if identity not in store.snapshot()["actions"]:
        knowledge.before_effect(actor["run_id"], actor["trial_id"])
    admitted = control.enqueue(task_id, request["text"], request["kind"], request["intent_id"],
                               expected=expected)
    receipt = {"input_id": admitted, "run_id": actor["run_id"],
               "trial_id": actor["trial_id"], "kind": request["kind"]}
    return cast(ManagedInputReceipt, validate_runtime(receipt, "ManagedInputReceipt"))
