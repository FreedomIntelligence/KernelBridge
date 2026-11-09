"""Filesystem authority adapters for the CLI-first KernelCat Wiki.

JSON/JSONL files remain the authority.  This module contains no MCP or CLI
knowledge; both transports use the same repository, evidence, page, raw-source,
and trajectory implementations.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    sha256_digest,
    validate_contract,
    validate_evidence_ref,
    validate_source_record,
    validate_trajectory_delta,
)
from kernelcat_knowledge.evidence.source_content import (
    SourceContentError,
    inspect_source_content,
    read_evidence_content,
)
from kernelcat_knowledge.ports import ArtifactResolver
from kernelcat_knowledge.resources.binding import ResourceBinding, normalized_path
from kernelcat_knowledge.storage.catalog_io import (
    catalog_lock,
    install,
    read_bytes,
    record_names,
)
from kernelcat_knowledge.storage.sources import source_record_paths

log = logging.getLogger(__name__)
_DEFAULT_Q_VALUE = 0.5
_V1_CARD_SCHEMA = "kernelcat.knowledge-card/v1"
_TEXT_EXTENSIONS = {
    "", ".md", ".txt", ".rst", ".h", ".hpp", ".hh", ".c", ".cc",
    ".cpp", ".cxx", ".py", ".yaml", ".yml", ".json", ".jsonl", ".sh", ".cmake",
}
_TEST_PARTS = {"test", "tests", "ut", "st", "example", "examples"}
_SAFE_DELTA_ID = re.compile(r"^td_[A-Za-z0-9_-]{1,120}$")
_REPRO_TO_MATURITY = {
    "concept": "source_derived",
    "pseudocode": "source_derived",
    "api-valid": "api_valid",
    "compile-tested": "compile_tested",
    "run-tested": "run_tested",
    "benchmarked": "benchmarked_local",
}


@dataclass(frozen=True)
class WikiPaths:
    repo_root: Path
    wiki_root: Path
    static_index: Path
    dynamic_index: Path
    card_root: Path | None
    rule_index: Path | None
    nav_index: Path
    raw_root: Path
    source_catalog: Path
    evidence_catalog: Path
    trajectory_delta_dir: Path
    sqlite_path: Path

    @classmethod
    def from_binding(cls, binding: ResourceBinding) -> WikiPaths:
        paths = binding.paths
        return cls(
            repo_root=paths.repo_root, wiki_root=paths.wiki_root,
            static_index=paths.static_index_path, dynamic_index=paths.dynamic_index_path,
            card_root=paths.card_index_path, rule_index=paths.rule_index_path,
            nav_index=paths.nav_index_path, raw_root=paths.raw_root,
            source_catalog=paths.source_records_dir,
            evidence_catalog=paths.repo_root / "data/evidence-catalog",
            trajectory_delta_dir=paths.trajectory_delta_dir,
            sqlite_path=paths.repo_root / "artifacts/kcwiki/read-model.sqlite3",
        )

    @classmethod
    def from_repo_root(cls, repo_root: Path | str | None = None) -> WikiPaths:
        from kernelcat_knowledge.resources.configuration import configured_resource_root

        root = configured_resource_root(repo_root)
        wiki = root / "wiki"
        return cls(
            repo_root=root,
            wiki_root=wiki,
            static_index=wiki / "guide/static_index.jsonl",
            dynamic_index=wiki / "guide/dynamic_index.jsonl",
            card_root=wiki / "cards",
            rule_index=wiki / "rules/index.jsonl",
            nav_index=wiki / "index.md",
            raw_root=root / "raw",
            source_catalog=root / "data/source-catalog",
            evidence_catalog=root / "data/evidence-catalog",
            trajectory_delta_dir=root / "raw/sessions/deltas",
            sqlite_path=root / "artifacts/kcwiki/read-model.sqlite3",
        )


def _normalize_path(path: str | Path) -> str:
    value = str(path).strip().replace("\\", "/")
    return value.removeprefix("./")


def _stable_revision(entry: Mapping[str, Any]) -> str:
    return "rev:" + sha256_digest(dict(entry))


def _is_revisioned_card(entry: Mapping[str, Any]) -> bool:
    return bool(
        entry.get("schema_version") == _V1_CARD_SCHEMA
        or entry.get("card_id")
        or entry.get("type") == "operator_card"
    )


def _render_v1_body(entry: Mapping[str, Any]) -> str:
    title = str(entry.get("title") or entry.get("card_id") or "Knowledge card")
    lines = [f"# {title}"]
    question = str(entry.get("decision_question") or "")
    summary = str(entry.get("summary") or "")
    if question:
        lines.extend(["", "## Decision question", question])
    if summary:
        lines.extend(["", "## Current conclusion", summary])
    applicability = entry.get("applicability")
    applicability = dict(applicability) if isinstance(applicability, Mapping) else {}
    if applicability:
        lines.extend([
            "", "## Applicability", "```json",
            json.dumps(applicability, ensure_ascii=False, sort_keys=True, indent=2),
            "```",
        ])
    reject_when = entry.get("reject_when") or applicability.get("reject_when") or []
    if reject_when:
        lines.extend(["", "## Reject when", *(f"- {item}" for item in reject_when)])
    lines.extend([
        "", "## Evidence state",
        f"- maturity: {entry.get('evidence_maturity', 'draft')}",
        f"- status: {entry.get('status', 'active')}",
        f"- revision: {entry.get('revision_id', '')}",
    ])
    return "\n".join(lines).strip()


def _normalize_entry(entry: Any, source_file: str, tier: str) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        log.warning("skip non-object index row (%s): %r", source_file, entry)
        return None
    row = dict(entry)
    if "ID" not in row:
        row["ID"] = row.get("id") or row.get("card_id")
    if not row.get("ID"):
        log.warning("skip index row without ID (%s)", source_file)
        return None

    if _is_revisioned_card(row):
        row.setdefault("card_id", str(row["ID"]))
        row["ID"] = str(row["card_id"])
        row.setdefault("id", row["ID"])
        row.setdefault("revision_id", _stable_revision(row))
        # Bind the original record without changing historical revision derivation.
        row["_authority_content_digest"] = sha256_digest(entry)
        row.setdefault("status", "active")
        row.setdefault(
            "card_type",
            "MechanismPolicy" if row.get("type") == "operator_card" else "OfficialFact",
        )
        row.setdefault(
            "evidence_maturity",
            _REPRO_TO_MATURITY.get(str(row.get("reproducibility") or ""), "draft"),
        )
        row.setdefault("type", "operator_card")
        row.setdefault("content_path", "wiki/cards/README.md")
        if row.get("schema_version") == _V1_CARD_SCHEMA:
            applicability = row.get("applicability")
            applicability = dict(applicability) if isinstance(applicability, Mapping) else {}
            task = row.get("task")
            task = dict(task) if isinstance(task, Mapping) else {}
            if applicability.get("operator") and not task.get("operator"):
                task["operator"] = applicability["operator"]
            for field in ("direction", "dtype", "layout"):
                if applicability.get(field) and not task.get(field):
                    task[field] = applicability[field]
            row["task"] = task
            row.setdefault("risks", list(row.get("reject_when") or applicability.get("reject_when") or []))
            row.setdefault("body", _render_v1_body(row))
        tier = "dynamic"

    row.setdefault("tags", [])
    row.setdefault("summary", "")
    row.setdefault("domain", "ascendc")
    row.setdefault("raw_links", [])
    row.setdefault("Q_value", _DEFAULT_Q_VALUE)
    row.setdefault("applicability", {})
    row.setdefault("evidence", [])
    row.setdefault("verification", {})
    row.setdefault("task", {})
    row.setdefault("decision", {})
    row.setdefault("hardware_plan", {})
    row.setdefault("risks", [])
    row.setdefault("reproducibility", "")
    row.setdefault("trust_contract", "")
    row["content_path"] = _normalize_path(row.get("content_path") or "")
    row["_tier"] = tier
    row["_index_source"] = source_file
    return row


def _load_jsonl(path: Path, tier: str, errors: list[str]) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{path}: read failed ({exc})")
        return []
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{line_no}: {exc.msg}")
            continue
        normalized = _normalize_entry(value, f"{path}:{line_no}", tier)
        if normalized is not None:
            rows.append(normalized)
    return rows


def _load_json(path: Path, tier: str, errors: list[str]) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path}: read/parse failed ({exc})")
        return []
    values = payload if isinstance(payload, list) else [payload]
    rows: list[dict[str, Any]] = []
    for index, value in enumerate(values, 1):
        normalized = _normalize_entry(value, f"{path}#{index}", tier)
        if normalized is not None:
            rows.append(normalized)
    return rows


def _load_path(path: Path, tier: str, errors: list[str]) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    return _load_json(path, tier, errors) if path.suffix.lower() == ".json" else _load_jsonl(path, tier, errors)


class IndexLoader:
    """Thread-safe active read repository with exact revision audit access."""

    def __init__(
        self,
        static_path: Path | None = None,
        dynamic_path: Path | None = None,
        *,
        card_path: Path | None = None,
        rule_path: Path | None = None,
        extra_paths: list[Path] | None = None,
        repo_root: Path | str | None = None,
    ) -> None:
        paths = WikiPaths.from_repo_root(repo_root)
        explicit = static_path is not None or dynamic_path is not None or bool(extra_paths)
        self._roots: list[tuple[Path, str]] = [
            (Path(static_path) if static_path is not None else paths.static_index, "static"),
            (Path(dynamic_path) if dynamic_path is not None else paths.dynamic_index, "dynamic"),
        ]
        card = Path(card_path) if card_path is not None else (None if explicit else paths.card_root)
        rule = Path(rule_path) if rule_path is not None else (None if explicit else paths.rule_index)
        if card is not None:
            self._roots.append((card, "dynamic"))
        if rule is not None:
            self._roots.append((rule, "rule"))
        for path in extra_paths or []:
            self._roots.append((Path(path), "dynamic"))

        self._lock = threading.Lock()
        self._loaded = False
        self._cached_entries: list[dict[str, Any]] = []
        self._cached_revisions: list[dict[str, Any]] = []
        self._cached_signature: tuple[tuple[str, int], ...] = ()
        self._path_index: dict[str, dict[str, Any]] = {}
        self._id_index: dict[str, dict[str, Any]] = {}
        self._revision_index: dict[str, dict[str, Any]] = {}
        self.active_index_revision = "sha256:" + "0" * 64
        self.load_errors: list[str] = []

    def _sources(self) -> list[tuple[Path, str]]:
        result: list[tuple[Path, str]] = []
        for root, tier in self._roots:
            if root.is_dir():
                paths = sorted([*root.glob("*.jsonl"), *root.glob("*.json")])
                result.extend((path, tier) for path in paths)
            else:
                result.append((root, tier))
        return result

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
    def _compile_active(
        rows: Sequence[dict[str, Any]],
        errors: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ordinary: dict[str, dict[str, Any]] = {}
        revisions: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in rows:
            if not _is_revisioned_card(entry):
                ordinary[str(entry["ID"])] = entry
                continue
            card_id = str(entry.get("card_id") or entry["ID"])
            revision_id = str(entry.get("revision_id") or _stable_revision(entry))
            revisions[(card_id, revision_id)] = entry

        by_card: dict[str, list[dict[str, Any]]] = {}
        for (card_id, _), entry in revisions.items():
            by_card.setdefault(card_id, []).append(entry)

        active_cards: list[dict[str, Any]] = []
        for card_id, card_revisions in sorted(by_card.items()):
            active = [row for row in card_revisions if str(row.get("status") or "active") == "active"]
            if len(active) > 1:
                errors.append(
                    f"card {card_id}: {len(active)} active revisions; all hidden until conflict resolution"
                )
                continue
            if active:
                active_cards.append(active[0])
        all_revisions = [row for _, row in sorted(revisions.items())]
        return [*ordinary.values(), *active_cards], all_revisions

    def load_all(self) -> list[dict[str, Any]]:
        with self._lock:
            sources = self._sources()
            signature = self._signature(sources)
            if self._loaded and signature == self._cached_signature:
                return list(self._cached_entries)
            errors: list[str] = []
            rows: list[dict[str, Any]] = []
            for path, tier in sources:
                rows.extend(_load_path(path, tier, errors))
            active_entries, revisions = self._compile_active(rows, errors)
            self._cached_entries = active_entries
            self._cached_revisions = revisions
            self._cached_signature = signature
            self._loaded = True
            self._path_index = {
                entry["content_path"]: entry
                for entry in active_entries
                if entry.get("content_path")
            }
            self._id_index = {str(entry["ID"]): entry for entry in active_entries}
            self._revision_index = {
                str(entry.get("revision_id")): entry
                for entry in revisions
                if entry.get("revision_id")
            }
            self._id_index.update(self._revision_index)
            fingerprint = sorted(
                [
                    str(entry.get("card_id") or entry["ID"]),
                    str(entry.get("revision_id") or ""),
                ]
                for entry in active_entries
                if _is_revisioned_card(entry)
            )
            self.active_index_revision = sha256_digest(fingerprint)
            self.load_errors = errors
            log.info(
                "index reloaded: active=%d revisions=%d sources=%d errors=%d",
                len(active_entries), len(revisions), len(sources), len(errors),
            )
            return list(active_entries)

    def card_snapshot(self) -> tuple[list[dict[str, Any]], str]:
        """Copy one cache generation and its revision under the existing loader lock.

        A concurrent reload may finish between load_all and this lock; both values
        below then belong to that newer generation. Later publication cannot append
        cards to the query input after its denial decision.
        """
        self.load_all()
        with self._lock:
            entries = [
                entry for entry in self._cached_entries if entry.get("_tier") == "dynamic"
            ]
            return deepcopy(entries), self.active_index_revision

    def load_all_revisions(self) -> list[dict[str, Any]]:
        self.load_all()
        return list(self._cached_revisions)

    def load_tier(self, tier: str) -> list[dict[str, Any]]:
        return [entry for entry in self.load_all() if entry.get("_tier") == tier]

    def find_by_path(self, path: str | Path) -> dict[str, Any] | None:
        self.load_all()
        return self._path_index.get(_normalize_path(path))

    def find_by_id(self, knowledge_id: str) -> dict[str, Any] | None:
        self.load_all()
        return self._id_index.get(str(knowledge_id))


class _ContractCatalog:
    def __init__(self, root: Path, *, id_key: str, schema_version: str) -> None:
        self.root = Path(root)
        self.id_key = id_key
        self.schema_version = schema_version

    def _path(self, object_id: str) -> Path:
        digest = hashlib.sha256(object_id.encode("utf-8")).hexdigest()[:32]
        return self.root / f"{digest}.json"

    def _paths(self, object_id: str) -> tuple[Path, ...]:
        if self.id_key == "source_id":
            return source_record_paths(self.root, object_id)
        return (self._path(object_id),)

    def _read(self, path: Path) -> dict[str, Any]:
        payload = validate_contract(json.loads(read_bytes(path, 16 * 1024 * 1024)))
        canonical_json(payload)
        if (payload.get("schema_version") != self.schema_version
                or path not in self._paths(str(payload.get(self.id_key) or ""))):
            raise ContractError("catalog schema or filename identity mismatch")
        return payload

    def register(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_contract(payload)
        if normalized.get("schema_version") != self.schema_version:
            raise ContractError(
                f"$.schema_version: expected {self.schema_version!r}, got {normalized.get('schema_version')!r}"
            )
        object_id = str(normalized[self.id_key])
        try:
            with catalog_lock(self.root):
                existing = self.get(object_id, strict=True)
                if existing is not None:
                    if canonical_json(existing) == canonical_json(normalized):
                        return {"status": "duplicate", self.id_key: object_id,
                                "digest": sha256_digest(normalized)}
                    return {
                        "status": "error", "code": "IDENTITY_CONFLICT",
                        self.id_key: object_id,
                        "message": f"{object_id} already exists with different canonical contents",
                    }
                install(self._path(object_id), (canonical_json(normalized) + "\n").encode("utf-8"),
                        mode=0o666)
        except OSError as exc:
            raise ContractError("catalog write failed") from exc
        return {
            "status": "registered", self.id_key: object_id,
            "digest": sha256_digest(normalized),
            "catalog_ref": f"{self.schema_version.removeprefix('kernelcat.').removesuffix('/v1')}://{object_id}",
        }

    def get(self, object_id: str, *, strict: bool = False) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        try:
            for path in self._paths(object_id):
                try:
                    payload = self._read(path)
                except FileNotFoundError:
                    continue
                if str(payload.get(self.id_key)) != object_id:
                    raise ContractError("catalog key identity mismatch")
                if found is not None and canonical_json(found) != canonical_json(payload):
                    raise ContractError("conflicting catalog aliases")
                found = payload
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            if strict:
                raise ContractError("catalog record is corrupt or unavailable") from exc
            return None
        return found

    def list(self, *, strict: bool = False) -> list[dict[str, Any]]:
        try:
            names = record_names(self.root)
        except OSError as exc:
            if strict:
                raise ContractError("catalog directory is unavailable") from exc
            return []
        rows: dict[str, dict[str, Any]] = {}
        conflicts: set[str] = set()
        for name in names:
            try:
                payload = self._read(self.root / name)
                object_id = str(payload[self.id_key])
                if object_id in rows and canonical_json(rows[object_id]) != canonical_json(payload):
                    conflicts.add(object_id)
                    raise ContractError("conflicting catalog aliases")
                rows[object_id] = payload
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                if strict:
                    raise ContractError("catalog record is corrupt or unavailable") from exc
        return [rows[key] for key in sorted(rows) if key not in conflicts]


class SourceCatalog:
    def __init__(self, root: Path, *, repo_root: Path, artifact_resolver: ArtifactResolver | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.artifact_resolver = artifact_resolver
        self._catalog = _ContractCatalog(
            root, id_key="source_id", schema_version="kernelcat.source/v1"
        )

    @property
    def root(self) -> Path:
        return self._catalog.root

    def register(self, record: Mapping[str, Any]) -> dict[str, Any]:
        validate_source_record(record)
        return self._catalog.register(record)

    def get(self, source_id: str) -> dict[str, Any] | None:
        return self._catalog.get(source_id)

    def list(self, *, source_class: str | None = None) -> list[dict[str, Any]]:
        rows = self._catalog.list()
        if source_class:
            rows = [row for row in rows if str(row.get("source_class")) == source_class]
        return rows

    def verify(self, source_id: str) -> dict[str, Any]:
        try:
            record = self._catalog.get(source_id, strict=True)
        except ContractError as exc:
            return {
                "status": "error", "code": "INVALID_CONTRACT",
                "source_id": source_id, "message": str(exc),
            }
        if record is None:
            return {"status": "error", "code": "NOT_FOUND", "source_id": source_id}
        return inspect_source_content(record, self.repo_root,
            artifact_reader=self.artifact_resolver.read_bytes if self.artifact_resolver else None)


class EvidenceCatalog:
    def __init__(self, root: Path) -> None:
        self._catalog = _ContractCatalog(
            root, id_key="evidence_id", schema_version="kernelcat.evidence/v1"
        )

    @property
    def root(self) -> Path:
        return self._catalog.root

    def register(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        validate_evidence_ref(evidence)
        return self._catalog.register(evidence)

    def get(self, evidence_id: str, *, strict: bool = False) -> dict[str, Any] | None:
        return self._catalog.get(evidence_id, strict=strict)

    def list(self, *, strict: bool = False) -> list[dict[str, Any]]:
        return self._catalog.list(strict=strict)


class TrajectoryOutboxStore:
    def __init__(self, outbox_dir: Path) -> None:
        self.outbox_dir = Path(outbox_dir)

    def append(self, delta: Mapping[str, Any]) -> dict[str, Any]:
        try:
            normalized = validate_trajectory_delta(delta)
        except ContractError as exc:
            return {"status": "error", "code": "INVALID_CONTRACT", "message": str(exc)}
        delta_id = str(normalized["trajectory_delta_id"])
        if not _SAFE_DELTA_ID.fullmatch(delta_id):
            return {
                "status": "error", "code": "INVALID_ID",
                "message": "trajectory_delta_id contains unsupported characters",
            }
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        target = self.outbox_dir / f"{delta_id}.json"
        payload = canonical_json(normalized) + "\n"
        if target.exists():
            try:
                existing = validate_trajectory_delta(
                    json.loads(target.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, ContractError) as exc:
                return {
                    "status": "error", "code": "CORRUPT_EXISTING_DELTA",
                    "message": str(exc), "trajectory_delta_id": delta_id,
                }
            if existing["digest"] == normalized["digest"]:
                return {
                    "status": "duplicate", "trajectory_delta_id": delta_id,
                    "digest": normalized["digest"],
                    "outbox_ref": f"trajectory-delta://{delta_id}",
                }
            return {
                "status": "error", "code": "IDEMPOTENCY_CONFLICT",
                "message": "the same trajectory_delta_id was already used for different contents",
                "trajectory_delta_id": delta_id,
            }
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, target)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            return {"status": "error", "code": "WRITE_FAILED", "message": str(exc)}
        return {
            "status": "appended", "trajectory_delta_id": delta_id,
            "episode_id": normalized["episode_id"], "digest": normalized["digest"],
            "terminal": normalized["terminal"],
            "outbox_ref": f"trajectory-delta://{delta_id}",
        }

    def status(self) -> dict[str, Any]:
        paths = sorted(self.outbox_dir.glob("td_*.json")) if self.outbox_dir.is_dir() else []
        corrupt: list[str] = []
        terminal = 0
        for path in paths:
            try:
                row = validate_trajectory_delta(json.loads(path.read_text(encoding="utf-8")))
                terminal += int(bool(row.get("terminal")))
            except (OSError, json.JSONDecodeError, ContractError):
                corrupt.append(path.name)
        return {
            "status": "ok" if not corrupt else "degraded",
            "count": len(paths),
            "terminal_count": terminal,
            "corrupt": corrupt,
            "outbox_dir": str(self.outbox_dir),
        }


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end_idx is None:
        return {}, text
    try:
        data = yaml.safe_load("\n".join(lines[1:end_idx])) or {}
        if not isinstance(data, dict):
            data = {}
    except yaml.YAMLError:
        data = {}
    return data, "\n".join(lines[end_idx + 1:])


_CARD_FIELDS = (
    "id", "card_id", "revision_id", "status", "card_type", "type",
    "trust_contract", "title", "tags", "domain", "reproducibility",
    "evidence_maturity", "fact_confidence", "attribution_confidence",
    "task", "applicability", "decision", "hardware_plan", "risks",
    "evidence", "evidence_refs", "claims", "verification",
    "performance_claims", "version_sensitive", "artifact_dir",
)


def _entry_overlay(entry: Mapping[str, Any]) -> dict[str, Any]:
    overlay = {
        key: entry[key]
        for key in _CARD_FIELDS
        if entry.get(key) not in (None, "", [], {})
    }
    if entry.get("ID") and "id" not in overlay:
        overlay["id"] = entry["ID"]
    return overlay


class PageReader:
    def __init__(
        self,
        loader: IndexLoader,
        *,
        wiki_root: Path,
        nav_index_path: Path,
        repo_root: Path | None = None,
    ) -> None:
        self.loader = loader
        self.wiki_root = Path(wiki_root).resolve()
        self.repo_root = (repo_root or self.wiki_root.parent).resolve()
        self.nav_index_path = Path(nav_index_path)

    def _resolve_safe(self, raw_path: str) -> Path | None:
        if not raw_path:
            return None
        candidate = (self.repo_root / _normalize_path(raw_path)).resolve()
        try:
            candidate.relative_to(self.wiki_root)
        except ValueError:
            return None
        return candidate

    def _read_one(
        self,
        knowledge_id: str,
        *,
        body_only: bool = False,
        max_chars: int | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        entry = self.loader.find_by_id(knowledge_id)
        if entry is None:
            return None, {"id": knowledge_id, "error": "id not found"}
        resolved = self._resolve_safe(str(entry.get("content_path") or ""))
        if resolved is None:
            return None, {"id": knowledge_id, "error": "invalid content_path (outside wiki root)"}
        if not resolved.is_file():
            return None, {"id": knowledge_id, "error": "content file not found"}
        try:
            document = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return None, {"id": knowledge_id, "error": f"read failed: {exc}"}
        doc_frontmatter, doc_body = parse_frontmatter(document)
        frontmatter = {**doc_frontmatter, **_entry_overlay(entry)}
        synthetic = entry.get("body")
        if synthetic not in (None, ""):
            body = str(synthetic).strip()
            full_content = body
        else:
            body = doc_body
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
        evidence = evidence if isinstance(evidence, list) else [evidence]
        for row in evidence:
            if not isinstance(row, Mapping):
                continue
            refs.append({
                "source": str(row.get("source") or ""),
                "sourceId": str(row.get("source_id") or ""),
                "locator": str(row.get("locator") or ""),
                "kind": str(row.get("kind") or ""),
            })
        return refs

    def get_pages(
        self,
        ids: list[str],
        *,
        follow_sources: bool = False,
        body_only: bool = False,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        if not ids:
            return {"pages": [], "errors": []}
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            return {"pages": [], "errors": [{"id": str(ids), "error": "ids must be a list of strings"}]}
        pages: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for knowledge_id in ids:
            page, error = self._read_one(
                knowledge_id, body_only=body_only, max_chars=max_chars
            )
            if error:
                errors.append(error)
                continue
            assert page is not None
            refs = self._evidence_refs(page["frontmatter"])
            page["sourceRefs"] = refs
            if follow_sources:
                source_pages: list[dict[str, Any]] = []
                seen: set[str] = set()
                for ref in refs:
                    source_entry = None
                    if ref["sourceId"]:
                        source_entry = self.loader.find_by_id(ref["sourceId"])
                    if source_entry is None and ref["source"].startswith("wiki/"):
                        source_entry = self.loader.find_by_path(ref["source"])
                    if not source_entry or str(source_entry["ID"]) in seen:
                        continue
                    seen.add(str(source_entry["ID"]))
                    source_page, source_error = self._read_one(
                        str(source_entry["ID"]), body_only=body_only, max_chars=max_chars
                    )
                    if source_page:
                        source_pages.append(source_page)
                    elif source_error:
                        errors.append(source_error)
                page["sourcePages"] = source_pages
            pages.append(page)
        return {"pages": pages, "errors": errors}

    def get_index(self) -> dict[str, Any]:
        if not self.nav_index_path.exists():
            return {"deprecated": True, "error": f"nav index not found: {self.nav_index_path}"}
        try:
            return {"deprecated": True, "content": self.nav_index_path.read_text(encoding="utf-8")}
        except OSError as exc:
            return {"deprecated": True, "error": f"read failed: {exc}"}


def _role(path: Path) -> str:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if "op_host" in parts or "tiling" in name or "host" in name:
        return "host"
    if path.suffix.lower() in {".md", ".rst", ".txt"} or "doc" in parts:
        return "doc"
    if "op_kernel" in parts or "kernel" in name:
        return "kernel"
    if "guide" in parts:
        return "guide"
    return "other"


class RawReader:
    def __init__(
        self,
        *,
        raw_root: Path,
        repo_root: Path,
        loader: IndexLoader,
        max_lines: int = 400,
        max_bytes: int = 131072,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> None:
        self.raw_root = Path(raw_root).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.loader = loader
        self.max_lines = max(1, int(max_lines))
        self.max_bytes = max(1024, int(max_bytes))
        self.artifact_resolver = artifact_resolver

    def _safe(self, raw: str | Path) -> Path | None:
        path = Path(raw)
        candidate = path.resolve() if path.is_absolute() else (self.repo_root / path).resolve()
        try:
            candidate.relative_to(self.raw_root)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _source_values(entry: Mapping[str, Any]) -> list[str]:
        values = [str(item) for item in entry.get("raw_links") or []]
        evidence = entry.get("evidence") or []
        evidence = evidence if isinstance(evidence, list) else [evidence]
        for row in evidence:
            if isinstance(row, Mapping) and str(row.get("source") or "").startswith("raw/"):
                values.append(str(row["source"]))
        return values

    def _scope_roots(self, scope: str | None) -> tuple[list[Path], list[str]]:
        if not scope:
            return [self.raw_root], []
        roots: list[Path] = []
        entry = self.loader.find_by_id(scope)
        if entry:
            for raw in self._source_values(entry):
                path = self._safe(raw.split("#", 1)[0].strip())
                if path and path.exists():
                    roots.append(path if path.is_dir() else path.parent)
        if not roots:
            needle = scope.lower().replace("-", "_")
            if self.raw_root.is_dir():
                for path in self.raw_root.rglob("*"):
                    if path.is_dir() and needle in path.name.lower().replace("-", "_"):
                        roots.append(path)
                        if len(roots) >= 8:
                            break
        roots = list(dict.fromkeys(path.resolve() for path in roots))
        return (roots or [self.raw_root]), [normalized_path(path, self.repo_root) for path in roots]

    @staticmethod
    def _files(roots: Iterable[Path], include_tests: bool, glob: str | None) -> Iterable[Path]:
        seen: set[Path] = set()
        for root in roots:
            candidates = [root] if root.is_file() else root.rglob(glob or "*")
            for path in candidates:
                if path in seen or not path.is_file() or path.suffix.lower() not in _TEXT_EXTENSIONS:
                    continue
                seen.add(path)
                lowered = {part.lower() for part in path.parts}
                if not include_tests and lowered & _TEST_PARTS:
                    continue
                yield path

    def grep(
        self,
        pattern: str,
        scope: str | None = None,
        context: int = 4,
        glob: str | None = None,
        include_tests: bool = False,
        max_hits: int = 30,
        *,
        roots_override: Sequence[Path] | None = None,
    ) -> dict[str, Any]:
        if not pattern:
            return {"hits": [], "returned": 0, "total_lower_bound": 0, "truncated": False, "scope_resolved": [], "error": "empty pattern"}
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return {"hits": [], "returned": 0, "total_lower_bound": 0, "truncated": False, "scope_resolved": [], "error": f"invalid regex: {exc}"}
        context = max(0, min(int(context), 10))
        max_hits = max(1, min(int(max_hits), 100))
        if roots_override:
            roots = [path.resolve() for path in roots_override]
            scope_resolved = [normalized_path(path, self.repo_root) for path in roots]
        else:
            roots, scope_resolved = self._scope_roots(scope)
        hits: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        for path in self._files(roots, include_tests, glob):
            scanned += 1
            try:
                if not path.resolve(strict=True).is_relative_to(self.raw_root.resolve()):
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096] or len(raw) > 8 * 1024 * 1024:
                continue
            lines = raw.decode("utf-8", errors="ignore").splitlines()
            for index, line in enumerate(lines):
                if not regex.search(line):
                    continue
                start = max(0, index - context)
                end = min(len(lines), index + context + 1)
                hits.append({
                    "path": normalized_path(path, self.repo_root),
                    "line": index + 1,
                    "snippet": "\n".join(f"{n + 1}: {lines[n]}" for n in range(start, end)),
                    "role": _role(path),
                })
                if len(hits) >= max_hits:
                    truncated = True
                    break
            if truncated:
                break
        order = {"host": 0, "doc": 1, "guide": 2, "kernel": 3, "other": 4}
        hits.sort(key=lambda row: (order.get(row["role"], 9), row["path"], row["line"]))
        payload: dict[str, Any] = {
            "hits": hits,
            "returned": len(hits),
            "total_lower_bound": len(hits),
            "truncated": truncated,
            "scope_resolved": scope_resolved,
        }
        if scanned == 0:
            payload["warning"] = "scope resolved to no readable text files"
        return payload

    def grep_source(
        self,
        source: Mapping[str, Any],
        pattern: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        locator = str(source.get("locator") or "")
        raw_path = locator.removeprefix("file://").split("#", 1)[0].split("::", 1)[0]
        safe = self._safe(raw_path)
        if safe is None or not safe.exists():
            return {
                "hits": [], "returned": 0, "total_lower_bound": 0,
                "truncated": False, "scope_resolved": [],
                "error": "source locator is unavailable or outside raw/",
                "source_id": source.get("source_id"),
            }
        result = self.grep(pattern, roots_override=[safe], **kwargs)
        result["source_id"] = source.get("source_id")
        return result

    def read(self, path: str, start: int = 1, end: int | None = None) -> dict[str, Any]:
        safe = self._safe(path)
        if safe is None:
            return {"path": path, "error": "path must stay inside raw/"}
        if not safe.is_file():
            return {"path": path, "error": "file not found"}
        try:
            raw = safe.read_bytes()
        except OSError as exc:
            return {"path": path, "error": f"read failed: {exc}"}
        if b"\x00" in raw[:4096]:
            return {"path": path, "error": "binary file is not readable"}
        lines = raw.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(1, int(start))
        requested_end = total if end is None else max(start, int(end))
        bounded_end = min(requested_end, start + self.max_lines - 1, total)
        content = "\n".join(lines[start - 1:bounded_end])
        encoded = content.encode("utf-8")
        byte_truncated = len(encoded) > self.max_bytes
        if byte_truncated:
            content = encoded[: self.max_bytes].decode("utf-8", errors="ignore")
        return {
            "path": normalized_path(safe, self.repo_root),
            "start": start,
            "end": bounded_end,
            "content": content,
            "total_lines": total,
            "truncated": byte_truncated or bounded_end < requested_end or bounded_end < total,
        }

    def read_evidence(
        self,
        evidence: Mapping[str, Any],
        *,
        source: Mapping[str, Any] | None = None,
        selector_override: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if source is None:
            return {"error": "Registered source is missing.", "code": "SOURCE_NOT_FOUND",
                    "evidence_id": evidence.get("evidence_id"), "integrity": "unverified"}
        try:
            return read_evidence_content(evidence, source, self.repo_root,
                selector_override=selector_override, max_lines=self.max_lines, max_bytes=self.max_bytes,
                artifact_reader=self.artifact_resolver.read_bytes if self.artifact_resolver else None)
        except SourceContentError as error:
            return {"error": str(error), "code": error.code, "integrity": "unverified",
                    "evidence_id": evidence.get("evidence_id"), "source_id": evidence.get("source_id")}
