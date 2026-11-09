"""Evidence operations over the existing immutable catalogue."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.ports import EvidenceRepository


def evidence_register(
    evidence: Mapping[str, Any], *, repository: EvidenceRepository
) -> dict[str, Any]:
    result = repository.register(evidence)
    return response(
        "kernelcat.evidence-operation/v1",
        str(result.get("status") or "error"),
        **{k: v for k, v in result.items() if k != "status"},
    )


def evidence_show(
    evidence_id: str, *, repository: EvidenceRepository
) -> dict[str, Any]:
    evidence = repository.get(evidence_id)
    if evidence is None:
        return response(
            "kernelcat.evidence-operation/v1",
            "error",
            code="NOT_FOUND",
            evidence_id=evidence_id,
        )
    return response("kernelcat.evidence-operation/v1", "ok", evidence=evidence)
