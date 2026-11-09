"""Filesystem SourceRecord catalog with immutable ID semantics."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kernelcat_knowledge.contracts import ContractError, canonical_json
from kernelcat_knowledge.ports import (
    CorruptRecordError,
    IntegrityError,
    RepositoryError,
)
from kernelcat_knowledge.storage.catalog_io import (
    catalog_lock,
    install,
    read_bytes,
    record_names,
)


def source_record_paths(root: Path, source_id: str) -> tuple[Path, Path]:
    """Both historical filenames remain readable; neither is renamed on access."""
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
    return root / (digest + ".json"), root / (digest[:32] + ".json")


class FilesystemSourceRepository:
    def __init__(self, records_dir: Path) -> None:
        self.records_dir = Path(records_dir)

    def _path(self, source_id: str) -> Path:
        return source_record_paths(self.records_dir, source_id)[0]

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(read_bytes(path, 16 * 1024 * 1024))
            if (not isinstance(payload, dict) or not isinstance(payload.get("source_id"), str)
                    or not payload["source_id"]):
                raise ValueError("SourceRecord must have a source_id")
            canonical_json(payload)
        except FileNotFoundError:
            raise
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise CorruptRecordError(f"{path}: invalid SourceRecord") from exc
        return payload

    def read(self, source_id: str) -> Mapping[str, Any] | None:
        found: dict[str, Any] | None = None
        for path in source_record_paths(self.records_dir, source_id):
            try:
                payload = self._read(path)
            except FileNotFoundError:
                continue
            if payload["source_id"] != source_id:
                raise CorruptRecordError(f"{path}: source_id does not match catalog key")
            if found is not None and canonical_json(found) != canonical_json(payload):
                raise CorruptRecordError("conflicting source catalog aliases")
            found = payload
        return found

    def list_records(self) -> list[Mapping[str, Any]]:
        try:
            names = record_names(self.records_dir)
        except OSError as exc:
            raise RepositoryError("source catalog is unavailable") from exc
        rows: dict[str, Mapping[str, Any]] = {}
        for name in names:
            path = self.records_dir / name
            try:
                payload = self._read(path)
            except FileNotFoundError as exc:
                raise CorruptRecordError("source catalog changed during listing") from exc
            source_id = payload["source_id"]
            if path not in source_record_paths(self.records_dir, source_id):
                raise CorruptRecordError(f"{path}: catalog filename does not match source_id")
            if source_id in rows and canonical_json(rows[source_id]) != canonical_json(payload):
                raise CorruptRecordError("conflicting source catalog aliases")
            rows[source_id] = payload
        return [rows[key] for key in sorted(rows)]

    def write_atomic(self, source_id: str, canonical_payload: str) -> None:
        try:
            payload = json.loads(canonical_payload)
            canonical_json(payload)
        except (ValueError, TypeError, RecursionError) as exc:
            raise RepositoryError("SourceRecord payload is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("source_id") != source_id:
            raise IntegrityError("SourceRecord payload source_id does not match catalog key")
        try:
            with catalog_lock(self.records_dir):
                existing = self.read(source_id)
                if existing is not None:
                    if canonical_json(existing) != canonical_json(payload):
                        raise IntegrityError("immutable source identity conflict")
                    return
                install(self._path(source_id), canonical_payload.encode("utf-8"), mode=0o666)
        except FileExistsError as exc:
            raise IntegrityError("immutable source identity conflict") from exc
        except (OSError, UnicodeError, ContractError) as exc:
            raise RepositoryError("source catalog write failed") from exc
