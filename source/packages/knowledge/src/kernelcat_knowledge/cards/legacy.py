"""Read-only normalization of historical operator cards."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts import sha256_digest

_REPRO_TO_MATURITY = {
    "concept": "source_derived",
    "pseudocode": "source_derived",
    "api-valid": "api_valid",
    "compile-tested": "compile_tested",
    "run-tested": "run_tested",
    "benchmarked": "benchmarked_local",
}


def _items(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def _legacy_evidence_ids(entry: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for row in _items(entry.get("evidence")):
        if not isinstance(row, Mapping):
            continue
        if row.get("evidence_id"):
            refs.append(str(row["evidence_id"]))
            continue
        identity = {
            "source": row.get("source"),
            "source_id": row.get("source_id"),
            "locator": row.get("locator"),
            "claim": row.get("claim"),
        }
        refs.append("ev:legacy:" + sha256_digest(identity).split(":", 1)[1][:24])
    refs.extend(
        str(item)
        for item in _items(entry.get("evidence_refs"))
        if item not in (None, "")
    )
    return list(dict.fromkeys(refs))


def adapt_legacy_card(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project a legacy operator card into the V1 read model without rewriting it."""
    task = (
        dict(entry.get("task") or {}) if isinstance(entry.get("task"), Mapping) else {}
    )
    applicability = (
        dict(entry.get("applicability") or {})
        if isinstance(entry.get("applicability"), Mapping)
        else {}
    )
    if task.get("operator"):
        applicability.setdefault("operator", task["operator"])
    for field in ("direction", "dtype", "layout"):
        if task.get(field):
            applicability.setdefault(field, task[field])
    if applicability.get("architecture") and not applicability.get("hardware"):
        applicability["hardware"] = applicability["architecture"]
    if applicability.get("soc") and not applicability.get("hardware"):
        applicability["hardware"] = applicability["soc"]
    if applicability.get("cann") and not applicability.get("toolchain"):
        applicability["toolchain"] = applicability["cann"]

    card_id = str(entry.get("card_id") or entry.get("ID") or entry.get("id") or "")
    title = str(entry.get("title") or entry.get("summary") or card_id)
    summary = str(entry.get("summary") or "")
    refs = _legacy_evidence_ids(entry)
    claim = {
        "claim_id": f"claim:legacy:{card_id}:summary",
        "subject": card_id,
        "predicate": "mechanism_summary",
        "object": summary,
        "authority_scope": "legacy_knowledge",
        "evidence_refs": refs,
        "confidence": "unknown",
        "validity": applicability,
    }
    question = (
        str(entry.get("decision_question") or "")
        or f"{title} 在当前硬 scope 下是否可作为下一步机制？"
    )
    revision_id = str(entry.get("revision_id") or "") or "rev:" + sha256_digest(
        dict(entry)
    )
    return {
        "schema_version": "kernelcat.knowledge-card/v1",
        "card_id": card_id,
        "revision_id": revision_id,
        "card_type": str(entry.get("card_type") or "MechanismPolicy"),
        "title": title,
        "decision_question": question,
        "summary": summary,
        "claims": [claim] if refs else [],
        "applicability": applicability or {"operator": task.get("operator", "unknown")},
        "use_when": list(applicability.get("use_when") or []),
        "reject_when": list(applicability.get("reject_when") or []),
        "evidence_refs": refs,
        "evidence_maturity": str(
            entry.get("evidence_maturity")
            or _REPRO_TO_MATURITY.get(str(entry.get("reproducibility") or ""), "draft")
        ),
        "fact_confidence": str(entry.get("fact_confidence") or "unknown"),
        "attribution_confidence": str(entry.get("attribution_confidence") or "none"),
        "status": str(entry.get("status") or "active"),
        "supersedes": list(entry.get("supersedes") or []),
        "depends_on": list(entry.get("depends_on") or []),
        "decision": dict(entry.get("decision") or {})
        if isinstance(entry.get("decision"), Mapping)
        else {},
        "tags": list(entry.get("tags") or []),
        "task": task,
        "legacy_evidence": [
            dict(row)
            for row in _items(entry.get("evidence"))
            if isinstance(row, Mapping)
        ],
        "created_at": str(entry.get("created_at") or "1970-01-01T00:00:00Z"),
        "updated_at": str(entry.get("updated_at") or "1970-01-01T00:00:00Z"),
    }
