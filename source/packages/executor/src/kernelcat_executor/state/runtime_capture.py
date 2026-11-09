"""Compact Knowledge admission/install metadata in the original Journal authority."""
from __future__ import annotations

from typing import TYPE_CHECKING, cast

from kernelcat_executor.contracts.knowledge_fences import (
    CAPTURE_INSTALLED_EVENT,
    CAPTURE_PREPARED_EVENT,
    MULTI_TRIAL_HEADER,
    registration_for,
    validate_knowledge,
)
from kernelcat_executor.contracts.models import Conflict, Json
from kernelcat_executor.contracts.runtime import require, validate_runtime
from kernelcat_executor.state.runtime_knowledge import check_trial_fence, trial_key
from kernelcat_executor.state.runtime_state import clone
from kernelcat_executor.state.runtime_trials import active_preparation

if TYPE_CHECKING:
    from kernelcat_executor.state.runtime import RuntimeJournal


def preparation_identity(record: Json) -> tuple[str, str]:
    observed = record["prepared"]
    if "request" in observed:
        request = observed["request"]
        return request["expected"]["run_id"], request["trial_id"]
    return record["manifest"]["run_id"], record["manifest"]["trial_id"]


def by_intent(state: Json, intent_id: str) -> Json:
    record = state["runtime_prepared"].get(intent_id) or state["runtime_trial_prepared"].get(intent_id)
    if record is None:
        raise Conflict("Knowledge request has no completed original preparation")
    return cast(Json, record)


def check_capture_account(state: Json, consumer: Json, run_id: str, trial_id: str) -> None:
    account = state["account"]
    if not account.get("multi_trial") or "knowledge_authority_id" not in account:
        raise Conflict("Knowledge capture requires a pinned explicit fresh v3 account")
    require(consumer, {"consumer_id": consumer["consumer_id"],
        "journal_authority_digest": account["authority_digest"], "account_id": account["account_id"],
        "run_id": run_id, "trial_id": trial_id, "required_journal_header": MULTI_TRIAL_HEADER},
        "Knowledge capture consumer identity/header")


def prepare_knowledge(journal: RuntimeJournal, value: Json) -> Json:
    request = validate_runtime(value, "PrepareKnowledgeCaptureRequest")
    validate_knowledge(request["capture_request"], "KnowledgeCaptureRequest")

    def apply(rows: list[Json]) -> tuple[Json | None, Json]:
        state = journal._state(rows)
        record = by_intent(state, request["preparation_intent_id"])
        run_id, trial_id = preparation_identity(record)
        key = trial_key(run_id, trial_id)
        old = state["runtime_capture_requests"].get(key)
        if old is not None:
            require(old, request, "complete original Knowledge capture envelope")
            return None, clone(old)
        active_preparation(state, record)
        if request["expected_account_revision"] != state["account_revision"]:
            raise Conflict("stale account revision before Knowledge capture admission")
        check_capture_account(state, request["capture_request"]["consumer"], run_id, trial_id)
        check_trial_fence(state, run_id, trial_id)
        for previous in state["runtime_capture_requests"].values():
            capture, current = previous["capture_request"], request["capture_request"]
            if (capture["intent_id"] == current["intent_id"] or
                    capture["consumer"]["consumer_id"] == current["consumer"]["consumer_id"]):
                raise Conflict("Knowledge capture/consumer identity already belongs to another Trial")
        event = {"kind": CAPTURE_PREPARED_EVENT, "request": request, "run_id": run_id, "trial_id": trial_id}
        return event, clone(request)
    return journal._transaction(apply)


def install_knowledge(journal: RuntimeJournal, value: Json, verified: Json) -> Json:
    request = validate_runtime(value, "InstallKnowledgeCaptureRequest")

    def apply(rows: list[Json]) -> tuple[Json | None, Json]:
        state = journal._state(rows)
        identity = request["preparation_intent_id"]
        old = state["runtime_capture_installs"].get(identity)
        if old is not None:
            require(old["request"], request, "original Knowledge install body")
            return None, clone(old["binding"])
        record = by_intent(state, identity)
        active_preparation(state, record)
        run_id, trial_id = preparation_identity(record)
        envelope = state["runtime_capture_requests"].get(trial_key(run_id, trial_id))
        if envelope is None:
            raise Conflict("Knowledge install has no original admitted capture")
        consumer = envelope["capture_request"]["consumer"]
        check_capture_account(state, consumer, run_id, trial_id)
        check_trial_fence(state, run_id, trial_id)
        expected = registration_for(envelope, state["account"]["knowledge_authority_id"])
        require(request["registration"], expected, "original installed registration")
        require(request["capture_request_digest"], expected["capture_request_digest"], "original capture digest")
        binding = {"schema_version": "kernelcat.trial-knowledge/v1", "registration": expected,
                   "view": request["view"], "capture_receipt_ref_id": request["capture_receipt_ref"]["ref_id"]}
        require(verified, binding, "verified immutable capture metadata")
        if len(record["required_ref_ids"]) >= 1024:
            raise Conflict("protected context reference capacity reached")
        event = {"kind": CAPTURE_INSTALLED_EVENT, "request": request, "binding": binding,
            "reference": request["capture_receipt_ref"], "run_id": run_id, "trial_id": trial_id}
        return event, clone(binding)
    return journal._transaction(apply)


def check_prepared_knowledge(state: Json, record: Json) -> None:
    if not state["account"].get("multi_trial"):
        return
    run_id, trial_id = preparation_identity(record)
    if "knowledge_authority_id" not in state["account"]:
        if "knowledge" in record["prepared"]:
            raise Conflict("unpinned v3 account cannot install Knowledge")
        return
    binding = record["prepared"].get("knowledge")
    if binding is None:
        raise Conflict("pinned v3 Trial requires its installed Knowledge capture before freeze")
    check_trial_fence(state, run_id, trial_id)
    envelope = state["runtime_capture_requests"].get(trial_key(run_id, trial_id))
    if envelope is None:
        raise Conflict("installed Knowledge capture has no original durable request")
    observed = record["prepared"]
    identity = observed["request"]["intent_id"] if "request" in observed else observed["intent_id"]
    require(envelope["preparation_intent_id"], identity, "capture original preparation")
    require(binding["registration"], registration_for(envelope, state["account"]["knowledge_authority_id"]),
            "prepared immutable Knowledge registration")
