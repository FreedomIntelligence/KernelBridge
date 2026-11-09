"""Thin cann-ask v5.2 adapter over the shared legacy-search application service."""
from __future__ import annotations

from kernelcat_knowledge.api.mcp import config
from kernelcat_knowledge.api.mcp.index_loader import IndexLoader, LazyLoader
from kernelcat_knowledge.generated.python_startup import (
    ExpectedPythonModules,
    admit_modules,
)
from kernelcat_knowledge.query.filters import AliasResolver
from kernelcat_knowledge.query.legacy import search_enveloped as _search_enveloped


async def search_enveloped(
    query: str,
    phase: str | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
    limit: int = 10,
    intent: str | None = None,
    progress: str | None = None,
    task_description: str | None = None,
    call_count: int = 0,
    seen_ids: list[str] | None = None,
    device_feedback: str | None = None,
    *,
    expected_modules: ExpectedPythonModules,
    mode: str | None = None,
    loader: IndexLoader | None = None,
    resolver: AliasResolver | None = None,
) -> dict:
    """Preserve the old call contract while delegating all search logic inward."""
    admit_modules(expected_modules, ("kernelcat_knowledge",))
    return await _search_enveloped(
        query=query,
        phase=phase,
        tags=tags,
        type=type,
        limit=limit,
        intent=intent,
        progress=progress,
        task_description=task_description,
        call_count=call_count,
        seen_ids=seen_ids,
        device_feedback=device_feedback,
        mode=config.CONFIG.search_mode if mode is None else mode,
        loader=LazyLoader(expected_modules=expected_modules) if loader is None else loader,
        resolver=AliasResolver(config.CONFIG.aliases_path) if resolver is None else resolver,
    )


async def search(
    query: str,
    tags: list[str] | None = None,
    domain: str | None = None,
    limit: int = 10,
    mode: str | None = None,
    loader: IndexLoader | None = None,
    *,
    expected_modules: ExpectedPythonModules,
) -> dict:
    """Legacy flat helper retained for existing tests and scripts."""
    envelope = await search_enveloped(
        expected_modules=expected_modules,
        query=query,
        tags=tags,
        type=domain,
        limit=limit,
        mode=mode,
        loader=loader,
        call_count=1,
    )
    tier = envelope["static"]
    results = [
        {
            "id": row["id"],
            "summary": row.get("summary", ""),
            "tags": row.get("tags", []),
            "score": row.get("score", 0.0),
            "qValue": float((row.get("extra") or {}).get("qValue") or 0.0),
        }
        for row in tier.get("results", [])
    ]
    payload = {"results": results, "total": tier.get("total", 0)}
    if tier.get("warning"):
        payload["warning"] = tier["warning"]
    return payload


__all__ = ["search", "search_enveloped"]
