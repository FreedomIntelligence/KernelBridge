"""Pure immutable registration/fence projection within the original Journal replay."""
from __future__ import annotations

from kernelcat_executor.contracts.knowledge_fences import fence_receipt, registration_for
from kernelcat_executor.contracts.models import Conflict, Json, canonical
from kernelcat_executor.contracts.runtime import require


def trial_key(run_id: str, trial_id: str) -> str:
    return canonical([run_id, trial_id])


def check_trial_fence(state: Json, run_id: str, trial_id: str) -> None:
    if trial_key(run_id, trial_id) in state["runtime_knowledge_trials"]:
        raise Conflict(f"Knowledge dependency permanently invalidated Run {run_id} Trial {trial_id}")


def check_registration(state: Json, request: Json) -> None:
    registration = request["registration"]
    consumer = registration["consumer"]
    envelope = state["runtime_capture_requests"].get(trial_key(consumer["run_id"], consumer["trial_id"]))
    if envelope is not None:
        require(registration, registration_for(envelope, state["account"]["knowledge_authority_id"]),
                "immutable admitted Knowledge registration")
    identities = (("runtime_knowledge_trials", trial_key(consumer["run_id"], consumer["trial_id"])),
                  ("runtime_knowledge_consumers", consumer["consumer_id"]),
                  ("runtime_knowledge_captures", registration["capture_intent_id"]))
    for collection, identity in identities:
        previous = state[collection].get(identity)
        if previous is not None:
            require(registration, previous, "immutable Knowledge registration")
    retraction = request["retraction"]
    previous = state["runtime_knowledge_retractions"].get(retraction["request"]["intent_id"])
    if previous is not None:
        require(retraction, previous, "immutable Knowledge retraction")


def apply_fence(state: Json, event: Json, row: Json) -> None:
    request = event["request"]
    check_registration(state, request)
    registration = request["registration"]
    consumer = registration["consumer"]
    state["runtime_knowledge_trials"][trial_key(consumer["run_id"], consumer["trial_id"])] = registration
    state["runtime_knowledge_consumers"][consumer["consumer_id"]] = registration
    state["runtime_knowledge_captures"][registration["capture_intent_id"]] = registration
    state["runtime_knowledge_retractions"][request["retraction"]["request"]["intent_id"]] = request["retraction"]
    state["runtime_knowledge_fences"][request["fence_id"]] = {
        "request": request, "receipt": fence_receipt(request, row)}
