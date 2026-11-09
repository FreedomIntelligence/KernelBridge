"""Generated Knowledge wire validation at the trusted Journal administration boundary."""
from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Iterator
from typing import Any, Final, cast

from jsonschema import Draft202012Validator, ValidationError, validators
from jsonschema.protocols import Validator

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.models import Conflict, Json, canonical, digest
from kernelcat_executor.contracts.runtime import require
from kernelcat_executor.generated.knowledge_runtime import (
    SCHEMA,
    KnowledgeFenceReceipt,
    KnowledgeFenceRequest,
)

KNOWLEDGE_HEADER = "runtime_account_initialized/v2"
MULTI_TRIAL_HEADER = "runtime_account_initialized/v3"
FENCE_EVENT = "runtime_knowledge_fence/v1"
FENCE_CONTROL_EVENT: Final = "runtime_knowledge_control/v1"
TRIAL_CONTROL_EVENT: Final = "runtime_trial_control/v1"
TRIAL_PREPARED_EVENT = "runtime_trial_prepared/v1"
TRIAL_FROZEN_EVENT = "runtime_trial_frozen/v1"
CAPTURE_PREPARED_EVENT = "runtime_knowledge_prepared/v1"
CAPTURE_INSTALLED_EVENT = "runtime_knowledge_installed/v1"


def _wire_json(value: object, depth: int = 0) -> None:
    if depth > 128:
        raise ValueError("Knowledge wire JSON exceeds 128 levels or contains a cycle")
    if type(value) is str:
        value.encode("utf-8", errors="strict")
    elif type(value) is int:
        if abs(value) > 9007199254740991:
            raise ValueError("Knowledge wire numbers must be safe integers")
    elif type(value) is float:
        if not math.isfinite(value) or int(value) != value or abs(value) > 9007199254740991:
            raise ValueError("Knowledge wire numbers must be safe integers")
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Knowledge wire object keys must be strings")
            _wire_json(key, depth + 1)
            _wire_json(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _wire_json(item, depth + 1)
    elif value is not None and type(value) is not bool:
        raise ValueError("Knowledge wire requires JSON values")


def _json_equality_key(value: object) -> Hashable:
    if type(value) is dict:
        return ("object", tuple(sorted((key, _json_equality_key(item)) for key, item in value.items())))
    if type(value) is list:
        return ("array", tuple(_json_equality_key(item) for item in value))
    if type(value) is bool:
        return ("boolean", value)
    if type(value) in {int, float}:
        return ("number", value)
    return ("scalar", value)


def _unique_items(validator: Any, enabled: bool, instance: object,
                  schema: Json) -> Iterator[ValidationError]:
    if not enabled or not validator.is_type(instance, "array"):
        return
    seen: set[Hashable] = set()
    for item in cast(list[object], instance):
        key = _json_equality_key(item)
        if key in seen:
            yield ValidationError("array has non-unique elements")
            return
        seen.add(key)


# The standard implementation compares unorderable dictionaries pairwise. The
# schema remains authoritative; this key retains JSON equality, including 1=1.0
# and true!=1, in O(payload bytes) space/time after the bounded JSON snapshot.
_extend_validator = cast(Callable[..., type[Validator]], validators.extend)
_KnowledgeValidator = _extend_validator(Draft202012Validator, {"uniqueItems": _unique_items})


def validate_knowledge(value: object, name: str) -> Json:
    _wire_json(value)
    validator = _KnowledgeValidator({**SCHEMA, "$ref": "#/$defs/" + name})
    return cast(Json, validated_snapshot(value, validator, name))


def fence_request(value: object) -> KnowledgeFenceRequest:
    request = cast(KnowledgeFenceRequest, validate_knowledge(value, "KnowledgeFenceRequest"))
    registration, retraction = request["registration"], request["retraction"]
    if registration["authority_id"] != retraction["authority_id"]:
        raise Conflict("Knowledge registration and retraction authorities differ")
    if retraction["request_digest"] != "sha256:" + digest(retraction["request"]):
        raise Conflict("Knowledge retraction request digest differs from its body")
    if retraction["control_revision"] != retraction["request"]["expected_control_revision"] + 1:
        raise Conflict("Knowledge retraction revision differs from its admitted request")
    body = {"registration": registration, "retraction": retraction}
    if request["fence_id"] != "sha256:" + digest(body):
        raise Conflict("Knowledge fence identity differs from its registration/retraction body")
    return request


def fence_receipt(request: Json, row: Json) -> KnowledgeFenceReceipt:
    receipt = {"schema_version": "kernelcat.knowledge-fence-receipt/v1",
        "fence_id": request["fence_id"], "request_digest": "sha256:" + digest(request),
        "registration": request["registration"], "journal_seq": row["seq"],
        "journal_event_digest": "sha256:" + row["digest"]}
    return cast(KnowledgeFenceReceipt, validate_knowledge(receipt, "KnowledgeFenceReceipt"))


def registration_for(envelope: Json, authority: str) -> Json:
    request = envelope["capture_request"]
    return {"schema_version": "kernelcat.knowledge-registration/v1", "authority_id": authority,
        "capture_intent_id": request["intent_id"], "capture_request_digest": "sha256:" + digest(request),
        "registration_epoch": 1, "consumer": request["consumer"]}



def verify_receipt(request: Json, envelope: Json, account: Json, receipt: Json, raw: bytes) -> Json:
    """Verify potentially large bytes outside every Journal transaction."""
    checked = validate_knowledge(receipt, "KnowledgeCaptureReceipt")
    if raw != canonical(checked).encode("utf-8"):
        raise Conflict("Knowledge capture receipt is not its canonical immutable UTF-8 bytes")
    expected = registration_for(envelope, account["knowledge_authority_id"])
    require(checked["registration"], expected, "actual capture receipt registration/request")
    require(request["registration"], expected, "installed capture registration/request")
    require(request["capture_request_digest"], expected["capture_request_digest"], "installed capture digest")
    require(request["view"], checked["view"], "actual capture receipt view")
    require(checked["view"]["authority_id"], expected["authority_id"], "capture view authority")
    require(checked["view"]["dependency_digest"], "sha256:" + digest(checked["dependencies"]),
            "capture receipt dependency closure")
    require(checked["view"]["member_count"], len(checked["dependencies"]), "capture dependency member count")
    if checked["control_revision"] < envelope["capture_request"]["expected_control_revision"]:
        raise Conflict("capture receipt predates its original control revision")
    reference = request["capture_receipt_ref"]
    identity = "knowledge-capture/" + reference["digest"].removeprefix("sha256:")
    require(reference["ref_id"], identity, "capture receipt reference label")
    require(reference["record_id"], identity, "capture receipt record identity")
    require(reference["owner"], "program_store", "capture receipt immutable owner")
    require(reference["digest_scope"], "exact UTF-8 knowledge-capture bytes", "capture receipt digest scope")
    return {"schema_version": "kernelcat.trial-knowledge/v1", "registration": expected,
        "view": checked["view"], "capture_receipt_ref_id": reference["ref_id"]}
