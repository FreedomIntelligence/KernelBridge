"""Resolve a card's evidence and source provenance without publishing it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.cards.legacy import adapt_legacy_card
from kernelcat_knowledge.contracts import validate_knowledge_card
from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.ports import (
    EvidenceRepository,
    KnowledgeRepository,
    WikiSourceRepository,
)


def _items(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def trace(
    knowledge_id: str,
    *,
    knowledge: KnowledgeRepository,
    sources: WikiSourceRepository,
    evidence_repository: EvidenceRepository,
) -> dict[str, Any]:
    entry = knowledge.find_by_id(knowledge_id)
    if entry is None:
        return response(
            "kernelcat.trace/v1", "error", code="NOT_FOUND", id=knowledge_id
        )
    card = (
        validate_knowledge_card(entry)
        if entry.get("schema_version") == "kernelcat.knowledge-card/v1"
        else adapt_legacy_card(entry)
    )
    refs = list(
        dict.fromkeys(
            [str(item) for item in _items(card.get("evidence_refs"))]
            + [
                str(item)
                for claim in _items(card.get("claims"))
                if isinstance(claim, Mapping)
                for item in _items(claim.get("evidence_refs"))
            ]
        )
    )
    evidence_rows: list[dict[str, Any]] = []
    source_rows: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for evidence_id in refs:
        evidence = evidence_repository.get(evidence_id)
        if evidence is None:
            missing.append(evidence_id)
            continue
        evidence_rows.append(evidence)
        source_id = str(evidence.get("source_id") or "")
        source = sources.get(source_id) if source_id else None
        if source:
            source_rows[source_id] = source
        elif source_id:
            missing.append(source_id)
    return response(
        "kernelcat.trace/v1",
        "ok",
        card={
            key: card.get(key)
            for key in (
                "card_id",
                "revision_id",
                "card_type",
                "title",
                "summary",
                "claims",
                "evidence_maturity",
                "fact_confidence",
                "attribution_confidence",
                "status",
            )
        },
        evidence=evidence_rows,
        sources=list(source_rows.values()),
        legacy_evidence=card.get("legacy_evidence") or [],
        missing_refs=list(dict.fromkeys(missing)),
    )
