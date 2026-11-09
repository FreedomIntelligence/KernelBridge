"""Deterministic validation for KernelCat Wiki V1 contract objects.

The repository intentionally avoids making an LLM or a database the authority for
identity, maturity, reward, or publication state. These checks are deliberately
small and dependency-free; JSON Schema files under ``wiki/schemas`` provide the
portable wire contract while this module enforces cross-field invariants.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from .canonical import content_revision_id, sha256_digest
from .history import validate_history_card
from .schema import SchemaError


class ContractError(ValueError):
    """Raised when a V1 contract object violates a deterministic invariant."""


CARD_TYPES = {
    "OfficialFact",
    "MechanismPolicy",
    "CausalGain",
    "Tombstone",
    "PlatformFact",
    "MeasurementProtocol",
    "OperatorHistory",
}
CARD_STATUSES = {
    "draft",
    "extracted",
    "validated",
    "review_required",
    "publishable",
    "active",
    "needs_revalidation",
    "superseded",
    "retracted",
}
EVIDENCE_MATURITY = {
    "draft",
    "source_derived",
    "api_valid",
    "compile_tested",
    "run_tested",
    "benchmarked_local",
    "formally_measured",
    "officially_landed",
    "retracted",
}
FACT_CONFIDENCE = {"high", "medium", "low", "unknown"}
ATTRIBUTION_CONFIDENCE = {"high", "medium_high", "candidate", "none"}
PHASES = {"correctness", "performance", "profiling", "formal", "platform", "all"}
_QUERY_AUTHORITY_FIELDS = frozenset({
    "schema_version", "operator", "operator_id", "mechanism_family", "phase",
    "hardware", "toolchain", "bottleneck", "decision_question", "task_envelope_ref",
    "predicates", "capabilities", "evidence_refs", "device_feedback", "seen_ids",
    "limit", "token_budget", "query_id",
})

_CARD_AUTHORITY_FIELDS = {
    "schema_version", "card_id", "card_type", "title", "decision_question",
    "summary", "claims", "applicability", "use_when", "reject_when",
    "evidence_refs", "evidence_maturity", "fact_confidence",
    "attribution_confidence", "status", "supersedes", "depends_on",
    "created_at", "updated_at", "policy_id", "mechanism_family",
    "revisit_when", "next_evidence_action", "reward_scope", "measurements",
    "formal", "rewrite", "source_claims",
}


def _copy_object(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError("$: contract payload must be an object")
    return deepcopy(dict(payload))


def _required(obj: Mapping[str, Any], key: str, expected: type | tuple[type, ...] | None = None) -> Any:
    if key not in obj or obj[key] in (None, ""):
        raise ContractError(f"$.{key}: required field is missing")
    value = obj[key]
    if expected is not None and not isinstance(value, expected):
        name = expected.__name__ if isinstance(expected, type) else "/".join(t.__name__ for t in expected)
        raise ContractError(f"$.{key}: expected {name}, got {type(value).__name__}")
    return value


def _string_list(value: Any, field: str, *, max_items: int | None = None) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"$.{field}: expected an array of strings")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ContractError(f"$.{field}[{index}]: expected a non-empty string")
        out.append(item)
    if max_items is not None and len(out) > max_items:
        raise ContractError(f"$.{field}: expected at most {max_items} items")
    return out


def _one_decision_question(value: Any, field: str = "decision_question") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"$.{field}: a non-empty decision question is required")
    question = " ".join(value.split())
    if len(question) > 300:
        raise ContractError(f"$.{field}: must not exceed 300 characters")
    if question.count("?") + question.count("？") > 1:
        raise ContractError(f"$.{field}: one query may contain only one decision question")
    if "\n" in value or "\r" in value:
        raise ContractError(f"$.{field}: must be a single line")
    return question


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"$.{field}: expected one of {sorted(allowed)}")
    return value


def _validate_evidence_id(value: str, field: str) -> None:
    if not value.startswith("ev:"):
        raise ContractError(f"$.{field}: evidence IDs must start with 'ev:'")


def validate_source_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    _required(obj, "source_id", str)
    _required(obj, "source_class", str)
    scopes = _string_list(_required(obj, "authority_scopes", Sequence), "authority_scopes")
    if not scopes:
        raise ContractError("$.authority_scopes: at least one authority scope is required")
    _required(obj, "locator", str)
    digest = _required(obj, "digest", str)
    if not digest.startswith("sha256:"):
        raise ContractError("$.digest: expected sha256:<hex>")
    _required(obj, "retrieved_at", str)
    scope = obj.get("digest_scope")
    if scope is not None and not isinstance(scope, str):
        raise ContractError("$.digest_scope: expected a string or null")
    status = str(obj.get("status") or "active")
    if status not in {"active", "superseded", "retracted"}:
        raise ContractError("$.status: expected active/superseded/retracted")
    obj["status"] = status
    return obj


def validate_evidence_ref(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    evidence_id = _required(obj, "evidence_id", str)
    _validate_evidence_id(evidence_id, "evidence_id")
    _required(obj, "source_id", str)
    digest = _required(obj, "artifact_digest", str)
    if not digest.startswith("sha256:"):
        raise ContractError("$.artifact_digest: expected sha256:<hex>")
    selector = _required(obj, "selector", Mapping)
    if not selector:
        raise ContractError("$.selector: a bounded selector is required")
    _required(obj, "observed_at", str)
    _required(obj, "producer", str)
    integrity = str(obj.get("integrity") or "verified")
    if integrity not in {"verified", "unverified", "corrupt"}:
        raise ContractError("$.integrity: expected verified/unverified/corrupt")
    obj["integrity"] = integrity
    return obj


def validate_claim(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    _required(obj, "claim_id", str)
    _required(obj, "subject", str)
    _required(obj, "predicate", str)
    if "object" not in obj:
        raise ContractError("$.object: required field is missing")
    _required(obj, "authority_scope", str)
    refs = _string_list(_required(obj, "evidence_refs", Sequence), "evidence_refs")
    if not refs:
        raise ContractError("$.evidence_refs: a claim requires at least one evidence reference")
    for index, evidence_id in enumerate(refs):
        _validate_evidence_id(evidence_id, f"evidence_refs[{index}]")
    confidence = str(obj.get("confidence") or "unknown")
    _enum(confidence, "confidence", FACT_CONFIDENCE)
    obj["confidence"] = confidence
    validity = obj.get("validity")
    if validity is not None and not isinstance(validity, Mapping):
        raise ContractError("$.validity: expected an object")
    return obj


def _reward_invariants(obj: Mapping[str, Any]) -> None:
    metrics = obj.get("metrics")
    if not isinstance(metrics, Mapping):
        return
    reward_source = str(metrics.get("reward_source") or "").lower()
    if reward_source == "pmu":
        raise ContractError("$.metrics.reward_source: PMU is mechanism evidence, not reward")
    if reward_source == "component_move" and not metrics.get("component_move_ref"):
        raise ContractError("$.metrics.component_move_ref: official reward requires ComponentMove evidence")


def validate_attempt_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    _required(obj, "event_id", str)
    _required(obj, "occurred_at", str)
    _required(obj, "campaign_id", str)
    _required(obj, "operator_id", str)
    _required(obj, "phase", str)
    _required(obj, "outcome", str)
    _required(obj, "reason_code", str)
    identity = _required(obj, "identity", Mapping)
    for key in ("parent_digest", "candidate_digest", "policy_id", "experiment_id"):
        if key in identity and identity[key] not in (None, "") and not isinstance(identity[key], str):
            raise ContractError(f"$.identity.{key}: expected a string or null")
    _string_list(obj.get("evidence_refs") or [], "evidence_refs")
    _reward_invariants(obj)
    note = obj.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        raise ContractError("$.note: expected a string no longer than 500 characters")
    return obj


def validate_trajectory_delta(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    delta_id = _required(obj, "trajectory_delta_id", str)
    if not delta_id.startswith("td_"):
        raise ContractError("$.trajectory_delta_id: expected td_<stable-id>")
    episode_id = _required(obj, "episode_id", str)
    if not episode_id.startswith("ep_"):
        raise ContractError("$.episode_id: expected ep_<stable-id>")
    event_ids = _string_list(_required(obj, "event_ids", Sequence), "event_ids")
    if not event_ids:
        raise ContractError("$.event_ids: at least one event is required")
    for index, event_id in enumerate(event_ids):
        if not event_id.startswith("evt_"):
            raise ContractError(f"$.event_ids[{index}]: expected evt_<stable-id>")
    _string_list(obj.get("result_refs") or [], "result_refs")
    _string_list(obj.get("evidence_refs") or [], "evidence_refs")
    if not isinstance(obj.get("terminal", False), bool):
        raise ContractError("$.terminal: expected a boolean")
    obj.setdefault("terminal", False)
    digest_payload = {key: value for key, value in obj.items() if key != "digest"}
    expected = sha256_digest(digest_payload)
    supplied = obj.get("digest")
    if supplied not in (None, "", expected):
        raise ContractError("$.digest: does not match canonical trajectory delta contents")
    obj["digest"] = expected
    return obj


def validate_knowledge_card(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    card_id = _required(obj, "card_id", str)
    if not card_id.startswith("kc:"):
        raise ContractError("$.card_id: expected kc:<type>:<scope>:<slug>:v<major>")
    _enum(_required(obj, "card_type", str), "card_type", CARD_TYPES)
    _required(obj, "title", str)
    obj["decision_question"] = _one_decision_question(_required(obj, "decision_question", str))
    _required(obj, "summary", str)
    claims = obj.get("claims") or []
    if not isinstance(claims, list):
        raise ContractError("$.claims: expected an array")
    obj["claims"] = [validate_claim(item) for item in claims]
    applicability = _required(obj, "applicability", Mapping)
    if not applicability:
        raise ContractError("$.applicability: executable knowledge must declare scope")
    refs = _string_list(obj.get("evidence_refs") or [], "evidence_refs")
    for index, evidence_id in enumerate(refs):
        _validate_evidence_id(evidence_id, f"evidence_refs[{index}]")
    maturity = _enum(_required(obj, "evidence_maturity", str), "evidence_maturity", EVIDENCE_MATURITY)
    status = _enum(_required(obj, "status", str), "status", CARD_STATUSES)
    obj["fact_confidence"] = _enum(str(obj.get("fact_confidence") or "unknown"), "fact_confidence", FACT_CONFIDENCE)
    obj["attribution_confidence"] = _enum(
        str(obj.get("attribution_confidence") or "none"),
        "attribution_confidence",
        ATTRIBUTION_CONFIDENCE,
    )
    _string_list(obj.get("supersedes") or [], "supersedes")
    _string_list(obj.get("depends_on") or [], "depends_on")
    if status in {"active", "needs_revalidation", "superseded"} and not refs:
        raise ContractError("$.evidence_refs: published knowledge requires evidence")
    if maturity == "officially_landed":
        formal = obj.get("formal")
        reward_scope = obj.get("reward_scope")
        if obj["card_type"] == "CausalGain" and reward_scope != "official_component":
            raise ContractError("$.reward_scope: officially landed CausalGain must use official_component")
        if obj["card_type"] == "CausalGain" and not isinstance(formal, Mapping):
            raise ContractError("$.formal: officially landed CausalGain requires formal component evidence")
    if obj["card_type"] == "CausalGain":
        reward_scope = str(obj.get("reward_scope") or "")
        if reward_scope not in {"local_event", "official_component", "incidental_component"}:
            raise ContractError("$.reward_scope: CausalGain requires local_event/official_component/incidental_component")
        formal = obj.get("formal") or {}
        if reward_scope == "local_event" and isinstance(formal, Mapping) and formal.get("official_delta") not in (None, {}):
            raise ContractError("$.formal.official_delta: local-only gain cannot claim official reward")
        if reward_scope == "incidental_component" and obj["attribution_confidence"] != "none":
            raise ContractError("$.attribution_confidence: incidental component gain must use none")
    measurements = obj.get("measurements")
    if isinstance(measurements, Mapping):
        _reward_invariants({"metrics": measurements})
    _required(obj, "created_at", str)
    _required(obj, "updated_at", str)
    if obj["card_type"] == "OperatorHistory":
        try:
            validate_history_card(obj)
        except (SchemaError, RecursionError) as error:
            raise ContractError(f"$.OperatorHistory: {error}") from error
    digest_payload = {key: obj[key] for key in sorted(_CARD_AUTHORITY_FIELDS) if key in obj}
    expected_revision = content_revision_id(digest_payload)
    supplied = obj.get("revision_id")
    if supplied not in (None, "", expected_revision):
        raise ContractError("$.revision_id: does not match canonical card contents")
    obj["revision_id"] = expected_revision
    return obj


def validate_query_predicates(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractError("$.predicates: expected an object")
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise ContractError("$.predicates: field names must be non-empty strings")
    reserved = sorted(set(value) & _QUERY_AUTHORITY_FIELDS)
    if reserved:
        raise ContractError(f"$.predicates: reserved query fields: {', '.join(reserved)}")
    if len(value) > 4:
        raise ContractError("$.predicates: use at most four hard scope predicates")
    return deepcopy(dict(value))


def validate_query_capabilities(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ContractError("$.capabilities: expected an array of observed capability names")
    return _string_list(value, "capabilities", max_items=64)


def validate_knowledge_query(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    operator_id = obj.get("operator_id")
    mechanism_family = obj.get("mechanism_family")
    if not operator_id and not mechanism_family:
        raise ContractError("$: operator_id or mechanism_family is required")
    if operator_id and mechanism_family:
        raise ContractError("$: query exactly one operator or one mechanism family")
    if operator_id is not None and not isinstance(operator_id, str):
        raise ContractError("$.operator_id: expected a string")
    if mechanism_family is not None and not isinstance(mechanism_family, str):
        raise ContractError("$.mechanism_family: expected a string")
    obj["phase"] = _enum(str(obj.get("phase") or "performance"), "phase", PHASES)
    _required(obj, "bottleneck", str)
    obj["decision_question"] = _one_decision_question(_required(obj, "decision_question", str))
    obj["predicates"] = validate_query_predicates(obj.get("predicates"))
    if "capabilities" in obj:
        obj["capabilities"] = validate_query_capabilities(obj["capabilities"])
    feedback = obj.get("device_feedback")
    if feedback is not None and (not isinstance(feedback, str) or len(feedback) > 200):
        raise ContractError("$.device_feedback: expected at most 200 characters")
    _string_list(obj.get("evidence_refs") or [], "evidence_refs", max_items=3)
    _string_list(obj.get("seen_ids") or [], "seen_ids")
    limit = int(obj.get("limit", 6))
    if not 1 <= limit <= 10:
        raise ContractError("$.limit: expected an integer in [1, 10]")
    obj["limit"] = limit
    budget = int(obj.get("token_budget", 1200))
    if not 256 <= budget <= 1200:
        raise ContractError("$.token_budget: expected an integer in [256, 1200]")
    obj["token_budget"] = budget
    return obj


def validate_context_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    _required(obj, "query_id", str)
    obj["decision_question"] = _one_decision_question(_required(obj, "decision_question", str))
    _required(obj, "scope", Mapping)
    for field in ("must_obey", "recommended_policies", "do_not_retry", "measurement_rules", "evidence_refs", "uncertainties"):
        if not isinstance(obj.get(field, []), list):
            raise ContractError(f"$.{field}: expected an array")
        obj.setdefault(field, [])
    _required(obj, "next_evidence_action", str)
    token_count = int(_required(obj, "token_count", int))
    if token_count > 1200:
        raise ContractError("$.token_count: ContextProjection must not exceed 1200 tokens")
    if not isinstance(obj.get("truncated", False), bool):
        raise ContractError("$.truncated: expected a boolean")
    obj.setdefault("truncated", False)
    return obj


_VALIDATORS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "kernelcat.source/v1": validate_source_record,
    "kernelcat.evidence/v1": validate_evidence_ref,
    "kernelcat.claim/v1": validate_claim,
    "kernelcat.attempt-event/v1": validate_attempt_event,
    "kernelcat.trajectory-delta/v1": validate_trajectory_delta,
    "kernelcat.knowledge-card/v1": validate_knowledge_card,
    "kernelcat.knowledge-query/v1": validate_knowledge_query,
    "kernelcat.context-projection/v1": validate_context_projection,
}


def validate_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    obj = _copy_object(payload)
    schema_version = _required(obj, "schema_version", str)
    validator = _VALIDATORS.get(schema_version)
    if validator is None:
        raise ContractError(f"$.schema_version: unsupported contract {schema_version!r}")
    return validator(obj)
