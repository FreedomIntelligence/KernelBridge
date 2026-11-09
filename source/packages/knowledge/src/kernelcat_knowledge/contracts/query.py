"""Structural V2 query boundary; admission and legacy payloads retain their owners."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from kernelcat_knowledge.contracts.runtime import validate_definition, wire_bytes
from kernelcat_knowledge.generated.knowledge_query import SCHEMA

T = TypeVar("T")


def validate_query_wire(value: T, definition: str) -> T:
    """Copy validated JSON; this does not certify provenance, ranks or V1 payloads."""
    validate_definition(value, SCHEMA, definition)
    return cast(T, json.loads(wire_bytes(value, allow_floats=True)))


def validate_query_receipt_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    from kernelcat_knowledge.contracts.validation import ContractError

    result = validate_query_wire(value, "QueryReceipt")
    rows = result["returned"]
    if [row["rank"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ContractError("query receipt ranks must be contiguous in returned order")
    if len({row["card_id"] for row in rows}) != len(rows):
        raise ContractError("query receipt contains duplicate card IDs")
    return result


def projection_membership(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    from kernelcat_knowledge.contracts.validation import ContractError

    rows = []
    for name in (
        "must_obey",
        "recommended_policies",
        "do_not_retry",
        "measurement_rules",
    ):
        if name not in projection or not isinstance(projection[name], list):
            raise ContractError("projection membership must contain all four arrays")
        for card in projection[name]:
            if not isinstance(card, dict) or not all(
                isinstance(card.get(key), str) and card[key]
                for key in ("card_id", "revision_id")
            ):
                raise ContractError("projection member lacks card/revision identity")
            rows.append(
                {
                    "card_id": card["card_id"],
                    "revision_id": card["revision_id"],
                    "rank": len(rows) + 1,
                }
            )
    return rows


def validate_search_result(
    value: Mapping[str, Any], *, query: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    from kernelcat_knowledge.contracts.canonical import sha256_digest
    from kernelcat_knowledge.contracts.validation import (
        ContractError,
        validate_contract,
    )
    from kernelcat_knowledge.query.projection import estimate_tokens

    result = validate_query_wire(value, "KnowledgeSearchResult")
    projection = result["projection"]
    if projection.get("schema_version") != "kernelcat.context-projection/v1":
        raise ContractError("search result requires a V1 context projection")
    validate_contract(projection)
    if (
        type(projection["token_count"]) is not int
        or projection["token_count"] < 1
        or projection["token_count"] != estimate_tokens(projection)
    ):
        raise ContractError("projection token estimate mismatch")
    expected_digest = sha256_digest(
        {
            key: item
            for key, item in projection.items()
            if key not in {"digest", "token_count"}
        }
    )
    if projection.get("digest") != expected_digest:
        raise ContractError("projection digest mismatch")
    receipt = validate_query_receipt_v2(result["receipt"])
    if receipt["returned"] != projection_membership(projection):
        raise ContractError("query receipt membership differs from final projection")
    if receipt["projection_digest"] != sha256_digest(projection) or receipt[
        "active_index_revision"
    ] != projection.get("active_index_revision"):
        raise ContractError("query receipt projection identity mismatch")
    if result["token_count"] != estimate_tokens(result):
        raise ContractError("search result token estimate mismatch")
    if query is not None:
        normalized = validate_contract(query)
        if (
            receipt["query_digest"] != sha256_digest(normalized)
            or projection["query_id"]
            != "q_" + sha256_digest(normalized).split(":")[1][:20]
            or len(receipt["returned"]) > normalized["limit"]
            or result["token_count"] > normalized["token_budget"]
        ):
            raise ContractError("query receipt does not match normalized request")
    return result
