"""Safe filesystem page adapter for Markdown and compact JSON cards."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from kernelcat_knowledge.ports import KnowledgeRepository
from kernelcat_knowledge.resources.configuration import load_repository_paths
from kernelcat_knowledge.storage.legacy_index import LOADER, _normalize_path

log = logging.getLogger(__name__)
_FRONTMATTER_DELIM = "---"
_DEFAULT_Q_VALUE = 0.5
_CARD_FIELDS = (
    "id",
    "type",
    "trust_contract",
    "title",
    "tags",
    "domain",
    "reproducibility",
    "task",
    "applicability",
    "decision",
    "hardware_plan",
    "risks",
    "evidence",
    "verification",
    "performance_claims",
    "version_sensitive",
    "artifact_dir",
)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith(_FRONTMATTER_DELIM):
        return {}, text
    lines = text.splitlines()
    end_idx = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == _FRONTMATTER_DELIM),
        None,
    )
    if end_idx is None:
        return {}, text
    try:
        data = yaml.safe_load("\n".join(lines[1:end_idx])) or {}
        if not isinstance(data, dict):
            data = {}
    except yaml.YAMLError as exc:
        log.warning("frontmatter YAML 解析失败: %s", exc)
        data = {}
    return data, "\n".join(lines[end_idx + 1 :])


def _entry_overlay(entry: Mapping[str, Any]) -> dict[str, Any]:
    overlay: dict[str, Any] = {}
    for key in _CARD_FIELDS:
        value = entry.get(key)
        if value not in (None, "", [], {}):
            overlay[key] = value
    if entry.get("ID") and "id" not in overlay:
        overlay["id"] = entry["ID"]
    return overlay


def _page_error(knowledge_id: str, code: str, message: str) -> dict[str, str]:
    return {"id": knowledge_id, "code": code, "error": message}


class PageReader:
    def __init__(
        self,
        loader: KnowledgeRepository | None = None,
        wiki_root: Path | None = None,
        nav_index_path: Path | None = None,
    ) -> None:
        if wiki_root is None or nav_index_path is None:
            defaults = load_repository_paths()
            wiki_root = wiki_root or defaults.wiki_root
            nav_index_path = nav_index_path or defaults.nav_index_path
        self._loader = loader or LOADER
        self._wiki_root = wiki_root.resolve()
        self._repo_root = self._wiki_root.parent.resolve()
        self._nav_index_path = nav_index_path

    def _resolve_safe(self, raw_path: str) -> Path | None:
        if not raw_path:
            return None
        candidate = (self._repo_root / _normalize_path(raw_path)).resolve()
        try:
            candidate.relative_to(self._wiki_root)
        except ValueError:
            return None
        return candidate

    def _read_one(
        self,
        knowledge_id: str,
        *,
        body_only: bool = False,
        max_chars: int | None = None,
    ) -> tuple[dict | None, dict | None]:
        entry = self._loader.find_by_id(knowledge_id)
        if entry is None:
            return None, _page_error(knowledge_id, "NOT_FOUND", "id not found")
        content_path = entry.get("content_path") or ""
        resolved = self._resolve_safe(content_path)
        if resolved is None:
            return None, _page_error(
                knowledge_id,
                "OUT_OF_BOUNDS",
                "invalid content_path (outside wiki root)",
            )
        if not resolved.is_file():
            return None, _page_error(
                knowledge_id,
                "NOT_FOUND",
                "content file not found",
            )
        try:
            document = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return None, _page_error(knowledge_id, "READ_FAILED", f"read failed: {exc}")

        document_frontmatter, document_body = parse_frontmatter(document)
        frontmatter = {**document_frontmatter, **_entry_overlay(entry)}
        synthetic_body = entry.get("body")
        if synthetic_body not in (None, ""):
            body = str(synthetic_body).strip()
            full_content = body
        else:
            body = document_body
            full_content = document
        content = body if body_only else full_content

        truncated = False
        if max_chars and max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars] + "\n…"
            truncated = True
        return {
            "id": knowledge_id,
            "frontmatter": frontmatter,
            "content": content,
            "qValue": float(entry.get("Q_value", _DEFAULT_Q_VALUE)),
            "truncated": truncated,
        }, None

    @staticmethod
    def _evidence_refs(frontmatter: Mapping[str, Any]) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        evidence = frontmatter.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [evidence]
        for row in evidence:
            if not isinstance(row, Mapping):
                continue
            refs.append(
                {
                    "source": str(row.get("source") or ""),
                    "sourceId": str(row.get("source_id") or ""),
                    "locator": str(row.get("locator") or ""),
                    "kind": str(row.get("kind") or ""),
                }
            )
        return refs

    def get_pages(
        self,
        ids: list[str],
        *,
        follow_sources: bool = False,
        body_only: bool = False,
        max_chars: int | None = None,
    ) -> dict:
        if not ids:
            return {"pages": [], "errors": []}
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            return {
                "pages": [],
                "errors": [
                    {
                        "id": str(ids),
                        "code": "INVALID_ARGUMENT",
                        "error": "ids must be a list of strings",
                    }
                ],
            }
        pages: list[dict] = []
        errors: list[dict] = []
        for knowledge_id in ids:
            page, error = self._read_one(
                knowledge_id,
                body_only=body_only,
                max_chars=max_chars,
            )
            if error:
                errors.append(error)
                continue
            assert page is not None
            refs = self._evidence_refs(page["frontmatter"])
            if refs:
                page["sourceRefs"] = refs
            if follow_sources and refs:
                source_pages: list[dict] = []
                seen: set[str] = set()
                for ref in refs:
                    source_entry = None
                    if ref["sourceId"]:
                        source_entry = self._loader.find_by_id(ref["sourceId"])
                    if source_entry is None and ref["source"].startswith("wiki/"):
                        source_entry = self._loader.find_by_path(ref["source"])
                    if not source_entry or source_entry["ID"] in seen:
                        continue
                    seen.add(source_entry["ID"])
                    source_page, source_error = self._read_one(
                        source_entry["ID"],
                        body_only=body_only,
                        max_chars=max_chars,
                    )
                    if source_page:
                        source_pages.append(source_page)
                    elif source_error:
                        errors.append(source_error)
                page["sourcePages"] = source_pages
            pages.append(page)
        return {"pages": pages, "errors": errors}

    def get_index(self) -> dict:
        if not self._nav_index_path.exists():
            return {
                "deprecated": True,
                "error": f"nav index not found: {self._nav_index_path}",
            }
        try:
            return {
                "deprecated": True,
                "content": self._nav_index_path.read_text(encoding="utf-8"),
            }
        except OSError as exc:
            return {"deprecated": True, "error": f"read failed: {exc}"}
