"""Source operations over the one authoritative source catalogue."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.ports import WikiSourceRepository


def source_register(
    record: Mapping[str, Any], *, sources: WikiSourceRepository
) -> dict[str, Any]:
    result = sources.register(record)
    return response(
        "kernelcat.source-operation/v1",
        str(result.get("status") or "error"),
        **{k: v for k, v in result.items() if k != "status"},
    )


def source_show(source_id: str, *, sources: WikiSourceRepository) -> dict[str, Any]:
    record = sources.get(source_id)
    if record is None:
        return response(
            "kernelcat.source-operation/v1",
            "error",
            code="NOT_FOUND",
            source_id=source_id,
        )
    return response("kernelcat.source-operation/v1", "ok", source=record)


def source_list(
    *, sources: WikiSourceRepository, source_class: str | None = None
) -> dict[str, Any]:
    records = sources.list(source_class=source_class)
    return response(
        "kernelcat.source-operation/v1",
        "ok",
        source_class=source_class,
        count=len(records),
        sources=records,
    )


def source_verify(source_id: str, *, sources: WikiSourceRepository) -> dict[str, Any]:
    result = sources.verify(source_id)
    return response(
        "kernelcat.source-operation/v1",
        str(result.get("status") or "error"),
        **{k: v for k, v in result.items() if k not in {"status", "digest"}},
    )
