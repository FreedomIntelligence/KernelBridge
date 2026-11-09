"""Legacy JSON/JSONL loader exposed as a rebuildable read-model adapter.

This module owns legacy normalization. MCP and CLI import the same adapter;
``mcp-server/index_loader.py`` is only a compatibility re-export.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterable
from pathlib import Path

from kernelcat_knowledge.resources.configuration import load_repository_paths

log = logging.getLogger(__name__)
_DEFAULT_Q_VALUE = 0.5
_V1_CARD_SCHEMA = "kernelcat.knowledge-card/v1"
_REPRO_TO_MATURITY = {
    "concept": "source_derived",
    "pseudocode": "source_derived",
    "api-valid": "api_valid",
    "compile-tested": "compile_tested",
    "run-tested": "run_tested",
    "benchmarked": "benchmarked_local",
}


def _normalize_path(path: str | Path) -> str:
    value = str(path).strip().replace("\\", "/")
    return value.removeprefix("./")


def _stable_revision(entry: dict) -> str:
    payload = json.dumps(
        entry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "rev:sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_revisioned_card(entry: dict) -> bool:
    return bool(
        entry.get("schema_version") == _V1_CARD_SCHEMA
        or entry.get("card_id")
        or entry.get("type") == "operator_card"
    )


def _render_v1_body(entry: dict) -> str:
    title = str(entry.get("title") or entry.get("card_id") or "Knowledge card")
    question = str(entry.get("decision_question") or "")
    summary = str(entry.get("summary") or "")
    lines = [f"# {title}"]
    if question:
        lines.extend(["", "## Decision question", question])
    if summary:
        lines.extend(["", "## Current conclusion", summary])
    applicability = entry.get("applicability") or {}
    if isinstance(applicability, dict) and applicability:
        lines.extend(
            [
                "",
                "## Applicability",
                "```json",
                json.dumps(applicability, ensure_ascii=False, sort_keys=True, indent=2),
                "```",
            ]
        )
    reject_when = list(entry.get("reject_when") or [])
    if not reject_when and isinstance(applicability, dict):
        reject_when = list(applicability.get("reject_when") or [])
    if reject_when:
        lines.extend(["", "## Reject when", *(f"- {item}" for item in reject_when)])
    lines.extend(
        [
            "",
            "## Evidence state",
            f"- maturity: {entry.get('evidence_maturity', 'draft')}",
            f"- status: {entry.get('status', 'active')}",
            f"- revision: {entry.get('revision_id', '')}",
        ]
    )
    return "\n".join(lines).strip()


def _normalize_entry(entry: dict, source_file: str, tier: str) -> dict | None:
    if not isinstance(entry, dict):
        log.warning("skip non-object index row (%s): %r", source_file, entry)
        return None
    entry = dict(entry)
    if "ID" not in entry:
        entry["ID"] = entry.get("id") or entry.get("card_id")
    if not entry.get("ID"):
        log.warning("skip index row without ID (%s)", source_file)
        return None

    if _is_revisioned_card(entry):
        entry.setdefault("card_id", str(entry["ID"]))
        entry["ID"] = str(entry["card_id"])
        entry.setdefault("id", entry["ID"])
        entry.setdefault("revision_id", _stable_revision(entry))
        entry.setdefault("status", "active")
        entry.setdefault(
            "card_type",
            "MechanismPolicy" if entry.get("type") == "operator_card" else "OfficialFact",
        )
        entry.setdefault(
            "evidence_maturity",
            _REPRO_TO_MATURITY.get(str(entry.get("reproducibility") or ""), "draft"),
        )
        entry.setdefault("type", "operator_card")
        entry.setdefault("content_path", "wiki/cards/README.md")
        if entry.get("schema_version") == _V1_CARD_SCHEMA:
            applicability = (
                dict(entry.get("applicability") or {})
                if isinstance(entry.get("applicability"), dict)
                else {}
            )
            task = dict(entry.get("task") or {}) if isinstance(entry.get("task"), dict) else {}
            if applicability.get("operator") and not task.get("operator"):
                task["operator"] = applicability["operator"]
            for field in ("direction", "dtype", "layout"):
                if applicability.get(field) and not task.get(field):
                    task[field] = applicability[field]
            entry["task"] = task
            entry.setdefault(
                "risks",
                list(entry.get("reject_when") or applicability.get("reject_when") or []),
            )
            entry.setdefault("body", _render_v1_body(entry))
        tier = "dynamic"

    entry.setdefault("tags", [])
    entry.setdefault("summary", "")
    entry.setdefault("domain", "ascendc")
    entry.setdefault("raw_links", [])
    entry.setdefault("Q_value", _DEFAULT_Q_VALUE)
    entry.setdefault("applicability", {})
    entry.setdefault("evidence", [])
    entry.setdefault("verification", {})
    entry.setdefault("task", {})
    entry.setdefault("decision", {})
    entry.setdefault("hardware_plan", {})
    entry.setdefault("risks", [])
    entry.setdefault("reproducibility", "")
    entry.setdefault("trust_contract", "")
    entry["content_path"] = _normalize_path(entry.get("content_path") or "")
    entry["_tier"] = tier
    entry["_index_source"] = source_file
    return entry


def _load_jsonl(path: Path, tier: str, errors: list[str]) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{path}: read failed ({exc})")
        return []
    out: list[dict] = []
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{line_no}: {exc.msg}")
            continue
        normalized = _normalize_entry(row, str(path), tier)
        if normalized is not None:
            out.append(normalized)
    return out


def _load_json(path: Path, tier: str, errors: list[str]) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path}: read/parse failed ({exc})")
        return []
    rows = payload if isinstance(payload, list) else [payload]
    out: list[dict] = []
    for index, row in enumerate(rows, 1):
        normalized = _normalize_entry(row, f"{path}#{index}", tier)
        if normalized is not None:
            out.append(normalized)
    return out


def _load_path(path: Path, tier: str, errors: list[str]) -> list[dict]:
    if not path.exists() or not path.is_file():
        return []
    return (
        _load_json(path, tier, errors)
        if path.suffix.lower() == ".json"
        else _load_jsonl(path, tier, errors)
    )


class IndexLoader:
    """Thread-safe mtime-cached active read model with revision audit access."""

    def __init__(
        self,
        static_path: Path | None = None,
        dynamic_path: Path | None = None,
        *,
        card_path: Path | None = None,
        rule_path: Path | None = None,
        extra_paths: list[Path] | None = None,
    ) -> None:
        using_repository_defaults = static_path is None and dynamic_path is None
        if static_path is None or dynamic_path is None:
            defaults = load_repository_paths()
            static_path = static_path or defaults.static_index_path
            dynamic_path = dynamic_path or defaults.dynamic_index_path
            if using_repository_defaults:
                card_path = card_path if card_path is not None else defaults.card_index_path
                rule_path = rule_path if rule_path is not None else defaults.rule_index_path
        self._roots: list[tuple[Path, str]] = [
            (static_path, "static"),
            (dynamic_path, "dynamic"),
        ]
        card = card_path
        rule = rule_path
        if card:
            self._roots.append((card, "dynamic"))
        if rule:
            self._roots.append((rule, "rule"))
        for path in extra_paths or []:
            self._roots.append((path, "dynamic"))

        self._lock = threading.Lock()
        self._cached_entries: list[dict] = []
        self._cached_revisions: list[dict] = []
        self._cached_signature: tuple[tuple[str, int], ...] = ()
        self._path_index: dict[str, dict] = {}
        self._id_index: dict[str, dict] = {}
        self._revision_index: dict[str, dict] = {}
        self.active_index_revision: str = "sha256:" + "0" * 64
        self.load_errors: list[str] = []

    def _sources(self) -> list[tuple[Path, str]]:
        out: list[tuple[Path, str]] = []
        for root, tier in self._roots:
            if root.is_dir():
                paths = sorted([*root.glob("*.jsonl"), *root.glob("*.json")])
                out.extend((path, tier) for path in paths)
            else:
                out.append((root, tier))
        return out

    def _signature(self, sources: Iterable[tuple[Path, str]]) -> tuple[tuple[str, int], ...]:
        tracked = [*(path for path, _ in self._roots), *(path for path, _ in sources)]
        values: list[tuple[str, int]] = []
        for path in tracked:
            try:
                mtime = os.stat(path).st_mtime_ns
            except FileNotFoundError:
                mtime = 0
            values.append((str(path), mtime))
        return tuple(values)

    @staticmethod
    def _compile_active(rows: list[dict], errors: list[str]) -> tuple[list[dict], list[dict]]:
        ordinary: dict[str, dict] = {}
        revisions: dict[tuple[str, str], dict] = {}
        for entry in rows:
            if not _is_revisioned_card(entry):
                ordinary[str(entry["ID"])] = entry
                continue
            card_id = str(entry.get("card_id") or entry["ID"])
            revision_id = str(entry.get("revision_id") or _stable_revision(entry))
            revisions[(card_id, revision_id)] = entry

        by_card: dict[str, list[dict]] = {}
        for (card_id, _), entry in revisions.items():
            by_card.setdefault(card_id, []).append(entry)

        active_cards: list[dict] = []
        for card_id, card_revisions in sorted(by_card.items()):
            active = [
                row
                for row in card_revisions
                if str(row.get("status") or "active") == "active"
            ]
            if len(active) > 1:
                errors.append(
                    f"card {card_id}: {len(active)} active revisions; all hidden until conflict resolution"
                )
                continue
            if active:
                active_cards.append(active[0])
        all_revisions = [row for _, row in sorted(revisions.items())]
        return [*ordinary.values(), *active_cards], all_revisions

    def invalidate(self) -> None:
        """Drop only the rebuildable cache; authority files remain untouched."""
        with self._lock:
            self._cached_entries = []
            self._cached_revisions = []
            self._cached_signature = ()
            self._path_index = {}
            self._id_index = {}
            self._revision_index = {}
            self.active_index_revision = "sha256:" + "0" * 64
            self.load_errors = []

    def load_all(self) -> list[dict]:
        with self._lock:
            sources = self._sources()
            signature = self._signature(sources)
            if signature == self._cached_signature and (
                self._cached_entries or self._cached_revisions
            ):
                return self._cached_entries
            errors: list[str] = []
            rows: list[dict] = []
            for path, tier in sources:
                rows.extend(_load_path(path, tier, errors))
            active_entries, revisions = self._compile_active(rows, errors)
            self._cached_entries = active_entries
            self._cached_revisions = revisions
            self._cached_signature = signature
            self._path_index = {
                entry["content_path"]: entry
                for entry in self._cached_entries
                if entry.get("content_path")
            }
            self._id_index = {str(entry["ID"]): entry for entry in self._cached_entries}
            self._revision_index = {
                str(entry.get("revision_id")): entry
                for entry in self._cached_revisions
                if entry.get("revision_id")
            }
            self._id_index.update(self._revision_index)
            active_fingerprint = [
                [
                    str(entry.get("card_id") or entry["ID"]),
                    str(entry.get("revision_id") or ""),
                ]
                for entry in self._cached_entries
                if _is_revisioned_card(entry)
            ]
            raw = json.dumps(
                active_fingerprint,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.active_index_revision = "sha256:" + hashlib.sha256(
                raw.encode("utf-8")
            ).hexdigest()
            self.load_errors = errors
            log.info(
                "index reloaded: active=%d revisions=%d sources=%d errors=%d",
                len(active_entries),
                len(revisions),
                len(sources),
                len(errors),
            )
            return self._cached_entries

    def load_all_revisions(self) -> list[dict]:
        self.load_all()
        return list(self._cached_revisions)

    def load_tier(self, tier: str) -> list[dict]:
        return [entry for entry in self.load_all() if entry.get("_tier") == tier]

    def find_by_path(self, path: str | Path) -> dict | None:
        self.load_all()
        return self._path_index.get(_normalize_path(path))

    def find_by_id(self, knowledge_id: str) -> dict | None:
        self.load_all()
        return self._id_index.get(knowledge_id)


class _LazyIndexLoader:
    """Resolve the repository default only on first use."""

    def __init__(self) -> None:
        self._loader: IndexLoader | None = None
        self._lock = threading.Lock()

    def _get(self) -> IndexLoader:
        if self._loader is None:
            with self._lock:
                if self._loader is None:
                    self._loader = IndexLoader()
        return self._loader

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


LOADER = _LazyIndexLoader()
