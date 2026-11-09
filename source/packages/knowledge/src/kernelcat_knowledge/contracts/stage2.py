"""Additive Stage 2 wire contracts for query provenance and evidence transport.

The original V1 validators remain stable in :mod:`kernelcat_knowledge.contracts.validation`.
This module extends the public dispatcher without rewriting that baseline.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from .canonical import sha256_digest
from .validation import (
    ContractError,
    validate_attempt_event,
    validate_evidence_ref,
    validate_source_record,
)
from .validation import (
    validate_contract as validate_v1_contract,
)

WIKI_MODES = {"local", "remote", "degraded"}
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_REF = re.compile(r"^(?:artifact|bundle|cas)://sha256/([0-9a-f]{64})$")


def _copy_object(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError("$: contract payload must be an object")
    return deepcopy(dict(payload))


def _required(
    obj: Mapping[str, Any],
    key: str,
    expected: type | tuple[type, ...] | None = None,
) -> Any:
    if key not in obj or obj[key] in (None, ""):
        raise ContractError(f"$.{key}: required field is missing")
    value = obj[key]
    if expected is not None and not isinstance(value, expected):
        name = (
            expected.__name__
            if isinstance(expected, type)
            else "/".join(item.__name__ for item in expected)
        )
        raise ContractError(f"$.{key}: expected {name}, got {type(value).__name__}")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"$.{field}: expected an array of strings")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ContractError(f"$.{field}[{index}]: expected a non-empty string")
        out.append(item)
    return out


def _object_list(value: Any, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"$.{field}: expected an array of objects")
    out: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ContractError(f"$.{field}[{index}]: expected an object")
        out.append(item)
    return out


def _reject_unknown(obj: Mapping[str, Any], allowed: set[str], field: str = "$") -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ContractError(f"{field}: unsupported fields {unknown}")


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ContractError(f"$.{field}: expected sha256:<64 lowercase hex>")
    return value


def _unique(values: Sequence[str], field: str) -> None:
    if len(set(values)) != len(values):
        raise ContractError(f"$.{field}: duplicate identities are not allowed")


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"$.{field}: expected one of {sorted(allowed)}")
    return value


def validate_query_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the provenance receipt for one KnowledgeQuery execution."""
    if payload.get("schema_version") == "kernelcat.query-receipt/v2":
        from kernelcat_knowledge.contracts.query import validate_query_receipt_v2

        return validate_query_receipt_v2(payload)
    obj = _copy_object(payload)
    _reject_unknown(
        obj,
        {
            "schema_version",
            "query_digest",
            "active_index_revision",
            "returned",
            "adopted_card_ids",
            "wiki_mode",
            "latency_ms",
            "projection_digest",
        },
    )
    if obj.get("schema_version") != "kernelcat.query-receipt/v1":
        raise ContractError("$.schema_version: expected 'kernelcat.query-receipt/v1'")
    obj["query_digest"] = _digest(
        _required(obj, "query_digest", str), "query_digest"
    )
    obj["active_index_revision"] = _digest(
        _required(obj, "active_index_revision", str), "active_index_revision"
    )

    returned: list[dict[str, Any]] = []
    ranks: list[int] = []
    identities: set[str] = set()
    for index, item in enumerate(
        _object_list(_required(obj, "returned", Sequence), "returned")
    ):
        _reject_unknown(item, {"card_id", "revision_id", "rank"}, f"$.returned[{index}]")
        card_id = _required(item, "card_id", str)
        revision_id = _required(item, "revision_id", str)
        rank = _required(item, "rank", int)
        if isinstance(rank, bool) or rank < 1:
            raise ContractError(f"$.returned[{index}].rank: expected a positive integer")
        if card_id in identities or revision_id in identities:
            raise ContractError(f"$.returned[{index}]: card/revision identity is duplicated")
        identities.update({card_id, revision_id})
        ranks.append(rank)
        returned.append({"card_id": card_id, "revision_id": revision_id, "rank": rank})
    if ranks and sorted(ranks) != list(range(1, len(ranks) + 1)):
        raise ContractError("$.returned.rank: ranks must be contiguous starting at 1")
    obj["returned"] = sorted(returned, key=lambda row: row["rank"])

    adopted = _string_list(
        _required(obj, "adopted_card_ids", Sequence), "adopted_card_ids"
    )
    _unique(adopted, "adopted_card_ids")
    if any(identity not in identities for identity in adopted):
        raise ContractError(
            "$.adopted_card_ids: each identity must appear in returned card_id/revision_id"
        )
    obj["adopted_card_ids"] = adopted
    obj["wiki_mode"] = _enum(
        _required(obj, "wiki_mode", str), "wiki_mode", WIKI_MODES
    )
    latency = _required(obj, "latency_ms", (int, float))
    if isinstance(latency, bool) or not math.isfinite(float(latency)) or latency < 0:
        raise ContractError("$.latency_ms: expected a finite non-negative number")
    obj["projection_digest"] = _digest(
        _required(obj, "projection_digest", str), "projection_digest"
    )
    return obj


def _validate_artifact_descriptor(
    payload: Mapping[str, Any], index: int
) -> dict[str, Any]:
    item = _copy_object(payload)
    _reject_unknown(
        item,
        {"ref", "artifact_digest", "schema_version", "selector", "required"},
        f"$.artifact_refs[{index}]",
    )
    ref = _required(item, "ref", str)
    digest = _digest(
        _required(item, "artifact_digest", str),
        f"artifact_refs[{index}].artifact_digest",
    )
    match = _ARTIFACT_REF.fullmatch(ref)
    if match and digest != f"sha256:{match.group(1)}":
        raise ContractError(
            f"$.artifact_refs[{index}]: ref digest and artifact_digest disagree"
        )
    schema_version = item.get("schema_version")
    if schema_version is not None and (
        not isinstance(schema_version, str) or not schema_version.strip()
    ):
        raise ContractError(
            f"$.artifact_refs[{index}].schema_version: expected a non-empty string or null"
        )
    selector = item.get("selector")
    if selector is not None and not isinstance(selector, Mapping):
        raise ContractError(
            f"$.artifact_refs[{index}].selector: expected an object or null"
        )
    required = _required(item, "required", bool)
    if not isinstance(required, bool):
        raise ContractError(f"$.artifact_refs[{index}].required: expected a boolean")
    return item


def validate_evidence_bundle(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the self-contained manifest referenced by a TrajectoryDelta."""
    obj = _copy_object(payload)
    _reject_unknown(
        obj,
        {
            "schema_version",
            "bundle_id",
            "producer",
            "created_at",
            "source_records",
            "evidence_refs",
            "query_receipts",
            "attempt_events",
            "artifact_refs",
            "digest",
        },
    )
    version = obj.get("schema_version")
    if version == "kernelcat.evidence-bundle/v2":
        from kernelcat_knowledge.contracts.query import validate_query_wire

        obj = validate_query_wire(obj, "EvidenceBundle")
    elif version != "kernelcat.evidence-bundle/v1":
        raise ContractError("$.schema_version: expected a supported evidence bundle version")
    bundle_id = _required(obj, "bundle_id", str)
    if not re.fullmatch(r"eb_[A-Za-z0-9_-]+", bundle_id):
        raise ContractError("$.bundle_id: expected eb_<stable-id>")
    _required(obj, "producer", str)
    _required(obj, "created_at", str)

    sources = [
        validate_source_record(item)
        for item in _object_list(
            _required(obj, "source_records", Sequence), "source_records"
        )
    ]
    source_ids = [str(item["source_id"]) for item in sources]
    _unique(source_ids, "source_records.source_id")
    for index, item in enumerate(sources):
        if item.get("schema_version") != "kernelcat.source/v1":
            raise ContractError(
                f"$.source_records[{index}].schema_version: expected 'kernelcat.source/v1'"
            )
        _digest(item.get("digest"), f"source_records[{index}].digest")
    obj["source_records"] = sources

    evidence = [
        validate_evidence_ref(item)
        for item in _object_list(
            _required(obj, "evidence_refs", Sequence), "evidence_refs"
        )
    ]
    evidence_ids = [str(item["evidence_id"]) for item in evidence]
    _unique(evidence_ids, "evidence_refs.evidence_id")
    for index, item in enumerate(evidence):
        if item.get("schema_version") != "kernelcat.evidence/v1":
            raise ContractError(
                f"$.evidence_refs[{index}].schema_version: expected 'kernelcat.evidence/v1'"
            )
        _digest(item.get("artifact_digest"), f"evidence_refs[{index}].artifact_digest")
    obj["evidence_refs"] = evidence

    receipts = [
        validate_query_receipt(item)
        for item in _object_list(
            _required(obj, "query_receipts", Sequence), "query_receipts"
        )
    ]
    expected_receipt = "kernelcat.query-receipt/" + version.rsplit("/", 1)[1]
    if any(item["schema_version"] != expected_receipt for item in receipts):
        raise ContractError("bundle and query receipt versions differ")
    identities = [
        sha256_digest({
            "binding": item["binding"], "query": item["query_digest"],
            "index": item["active_index_revision"], "projection": item["projection_digest"],
        }) if version.endswith("/v2") else
        f"{item['query_digest']}|{item['active_index_revision']}|{item['projection_digest']}"
        for item in receipts
    ]
    _unique(identities, "query_receipts.identity")
    obj["query_receipts"] = receipts

    events = [
        validate_attempt_event(item)
        for item in _object_list(
            _required(obj, "attempt_events", Sequence), "attempt_events"
        )
    ]
    for index, item in enumerate(events):
        if item.get("schema_version") != "kernelcat.attempt-event/v1":
            raise ContractError(
                f"$.attempt_events[{index}].schema_version: expected 'kernelcat.attempt-event/v1'"
            )
    _unique([str(item["event_id"]) for item in events], "attempt_events.event_id")
    obj["attempt_events"] = events

    artifacts = [
        _validate_artifact_descriptor(item, index)
        for index, item in enumerate(
            _object_list(_required(obj, "artifact_refs", Sequence), "artifact_refs")
        )
    ]
    _unique([str(item["ref"]) for item in artifacts], "artifact_refs.ref")
    obj["artifact_refs"] = artifacts

    expected = sha256_digest({key: value for key, value in obj.items() if key != "digest"})
    supplied = obj.get("digest")
    if supplied not in (None, "", expected):
        raise ContractError("$.digest: does not match canonical evidence bundle contents")
    obj["digest"] = expected
    return obj


_STAGE2_VALIDATORS: dict[
    str, Callable[[Mapping[str, Any]], dict[str, Any]]
] = {
    "kernelcat.query-receipt/v1": validate_query_receipt,
    "kernelcat.evidence-bundle/v1": validate_evidence_bundle,
    "kernelcat.evidence-bundle/v2": validate_evidence_bundle,
    "kernelcat.query-receipt/v2": validate_query_receipt,
}


def validate_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Dispatch Stage 2 contracts, then fall back to the stable V1 dispatcher."""
    obj = _copy_object(payload)
    schema_version = _required(obj, "schema_version", str)
    if schema_version == "kernelcat.knowledge-search-result/v2":
        from kernelcat_knowledge.contracts.query import validate_search_result

        return validate_search_result(obj)
    validator = _STAGE2_VALIDATORS.get(schema_version)
    return validator(obj) if validator is not None else validate_v1_contract(obj)
