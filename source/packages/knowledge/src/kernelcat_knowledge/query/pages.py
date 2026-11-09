"""Bounded views of logical pages and exact card revisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.ports import KnowledgeRepository, PageReaderPort


def _public(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not str(key).startswith("_")}


def show(
    knowledge_id: str,
    *,
    knowledge: KnowledgeRepository,
    page_reader: PageReaderPort,
    view: str = "agent",
    max_chars: int = 12000,
) -> dict[str, Any]:
    if view not in {"agent", "audit", "human"}:
        return response(
            "kernelcat.object-result/v1",
            "error",
            code="INVALID_ARGUMENT",
            message="view must be agent/audit/human",
        )
    entry = knowledge.find_by_id(knowledge_id)
    if entry is None:
        return response(
            "kernelcat.object-result/v1",
            "error",
            code="NOT_FOUND",
            id=knowledge_id,
        )
    if view == "human":
        result = page_reader.get_pages([knowledge_id], max_chars=max_chars)
        if result.get("errors"):
            return response(
                "kernelcat.object-result/v1",
                "error",
                code="READ_FAILED",
                id=knowledge_id,
                errors=result["errors"],
            )
        obj: Any = result["pages"][0]
    elif view == "audit":
        obj = _public(entry)
    else:
        obj = _public(entry)
        obj.pop("body", None)
        obj.pop("raw_links", None)
        obj.pop("content_path", None)
    return response(
        "kernelcat.object-result/v1",
        "ok",
        id=knowledge_id,
        view=view,
        object=obj,
    )


def get_page_legacy(
    knowledge_id: str, *, page_reader: PageReaderPort
) -> dict[str, Any]:
    result = page_reader.get_pages([knowledge_id], follow_sources=False)
    if result.get("errors"):
        error = result["errors"][0]
        return {"id": knowledge_id, "error": error.get("error", "page read failed")}
    pages = result.get("pages") or []
    if not pages:
        return {"id": knowledge_id, "error": "id not found"}
    page = pages[0]
    return {
        "id": knowledge_id,
        "frontmatter": page.get("frontmatter") or {},
        "content": page.get("content") or "",
        "qValue": float(page.get("qValue") or 0.0),
    }
