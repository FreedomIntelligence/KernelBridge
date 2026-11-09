"""Bounded source and EvidenceRef reads over injected readers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.ports import EvidenceRepository, WikiSourceRepository


def raw_grep(
    *,
    sources: WikiSourceRepository,
    raw_reader: Any,
    pattern: str,
    source_id: str | None = None,
    scope: str | None = None,
    context: int = 4,
    glob: str | None = None,
    include_tests: bool = False,
    max_hits: int = 30,
    legacy: bool = False,
) -> dict[str, Any]:
    if source_id:
        source = sources.get(source_id)
        if source is None:
            result = {
                "hits": [],
                "returned": 0,
                "truncated": False,
                "error": "source not found",
                "source_id": source_id,
            }
        else:
            result = raw_reader.grep_source(
                source,
                pattern,
                context=context,
                glob=glob,
                include_tests=include_tests,
                max_hits=max_hits,
            )
    else:
        result = raw_reader.grep(pattern, scope, context, glob, include_tests, max_hits)
    if legacy:
        return result
    status = "error" if result.get("error") else "ok"
    return response("kernelcat.raw-result/v1", status, result=result)


def raw_read(
    *,
    sources: WikiSourceRepository,
    evidence_repository: EvidenceRepository,
    raw_reader: Any,
    path: str | None = None,
    evidence_id: str | None = None,
    start: int = 1,
    end: int | None = None,
    selector_override: Mapping[str, Any] | None = None,
    legacy: bool = False,
) -> dict[str, Any]:
    if evidence_id:
        evidence = evidence_repository.get(evidence_id)
        if evidence is None:
            result = {"error": "evidence not found", "evidence_id": evidence_id}
        else:
            result = raw_reader.read_evidence(
                evidence,
                source=sources.get(str(evidence.get("source_id") or "")),
                selector_override=selector_override,
            )
    elif path:
        result = raw_reader.read(path, start, end)
    else:
        result = {"error": "path or evidence_id is required"}
    if legacy:
        return result
    status = "error" if result.get("error") else "ok"
    return response("kernelcat.raw-result/v1", status, result=result)
