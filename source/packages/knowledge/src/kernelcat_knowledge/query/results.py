"""Finalize bounded result bytes and provenance only after final membership selection."""

from __future__ import annotations

from typing import Any

from kernelcat_knowledge.contracts import ContractError, sha256_digest
from kernelcat_knowledge.contracts.query import (
    projection_membership,
    validate_search_result,
)
from kernelcat_knowledge.query.projection import estimate_tokens


def _measure(value: dict[str, Any]) -> None:
    value["token_count"] = 0
    while True:
        measured = estimate_tokens(value)
        if measured == value["token_count"]:
            return
        value["token_count"] = measured


def _trim(projection: dict[str, Any]) -> None:
    for field in (
        "recommended_policies",
        "measurement_rules",
        "must_obey",
        "do_not_retry",
        "uncertainties",
        "evidence_refs",
    ):
        if projection[field]:
            projection[field].pop()
            projection["truncated"] = True
            return
    compact = "补齐最小证据后重新查询。"
    if projection["next_evidence_action"] != compact:
        projection["next_evidence_action"] = compact
        projection["truncated"] = True
        return
    raise ContractError(
        "KnowledgeSearchResult fixed envelope exceeds requested token budget"
    )


def finish_result(
    projection: dict[str, Any],
    query: dict[str, Any],
    binding: dict[str, Any],
    latency_ms: float,
) -> dict[str, Any]:
    """At most ten card removals plus bounded metadata trimming; no corpus rescans."""
    while True:
        projection["digest"] = sha256_digest(
            {
                key: value
                for key, value in projection.items()
                if key not in {"digest", "token_count"}
            }
        )
        _measure(projection)
        receipt = {
            "schema_version": "kernelcat.query-receipt/v2",
            "binding": binding,
            "query_digest": sha256_digest(query),
            "active_index_revision": projection["active_index_revision"],
            "returned": projection_membership(projection),
            "adopted_card_ids": None,
            "wiki_mode": "local",
            "latency_ms": latency_ms,
            "projection_digest": sha256_digest(projection),
        }
        result = {
            "schema_version": "kernelcat.knowledge-search-result/v2",
            "projection": projection,
            "receipt": receipt,
            "token_count": 0,
        }
        _measure(result)
        if result["token_count"] <= query["token_budget"]:
            return validate_search_result(result, query=query)
        _trim(projection)
