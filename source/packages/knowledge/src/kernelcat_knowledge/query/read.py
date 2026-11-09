"""Transport-neutral read use-cases over page/raw/index ports."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.ports import IndexReadModel, PageReaderPort, RawReaderPort
from kernelcat_knowledge.ports.errors import ApplicationError


def _coded_failure(
    error: Mapping[str, Any] | None,
    *,
    fallback_code: str,
    fallback_message: str,
) -> ApplicationError:
    """Translate adapter error codes without coupling behavior to prose."""
    row = error or {}
    code = str(row.get("code") or fallback_code)
    message = str(row.get("error") or fallback_message)
    exit_code = 2 if code == "INVALID_ARGUMENT" else 3 if code == "NOT_FOUND" else 6
    return ApplicationError(code, message, exit_code=exit_code)


def show_page(
    knowledge_id: str,
    *,
    reader: PageReaderPort,
    follow_sources: bool = False,
    body_only: bool = False,
    max_chars: int | None = None,
) -> dict[str, Any]:
    result = reader.get_pages(
        [knowledge_id],
        follow_sources=follow_sources,
        body_only=body_only,
        max_chars=max_chars,
    )
    pages = result.get("pages") or []
    if pages:
        return pages[0]
    errors = result.get("errors") or []
    first = errors[0] if errors and isinstance(errors[0], Mapping) else None
    raise _coded_failure(
        first,
        fallback_code="PAGE_READ_FAILED",
        fallback_message="page reader returned no page",
    )


def show_card_audit(knowledge_id: str, *, index: IndexReadModel) -> dict[str, Any]:
    entry = index.find_by_id(knowledge_id)
    if entry is None:
        raise ApplicationError("NOT_FOUND", "id not found", exit_code=3)
    return {key: value for key, value in entry.items() if not str(key).startswith("_")}


def grep_raw(
    pattern: str,
    *,
    reader: RawReaderPort,
    scope: str | None = None,
    context: int = 4,
    glob: str | None = None,
    include_tests: bool = False,
    max_hits: int = 30,
) -> dict[str, Any]:
    result = reader.grep(pattern, scope, context, glob, include_tests, max_hits)
    if result.get("error"):
        raise _coded_failure(
            result,
            fallback_code="RAW_QUERY_FAILED",
            fallback_message="raw query failed",
        )
    return result


def read_raw(
    path: str,
    *,
    reader: RawReaderPort,
    start: int = 1,
    end: int | None = None,
) -> dict[str, Any]:
    result = reader.read(path, start, end)
    if result.get("error"):
        raise _coded_failure(
            result,
            fallback_code="RAW_READ_FAILED",
            fallback_message="raw read failed",
        )
    return result


def rebuild_index(*, index: IndexReadModel) -> dict[str, Any]:
    index.invalidate()
    active = index.load_all()
    revisions = index.load_all_revisions()
    errors = list(index.load_errors)
    return {
        "schema_version": "kernelcat.index-rebuild/v1",
        "status": "ok" if not errors else "degraded",
        "active_index_revision": index.active_index_revision,
        "active_entries": len(active),
        "card_revisions": len(revisions),
        "errors": errors,
    }
