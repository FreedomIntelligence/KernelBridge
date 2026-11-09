"""Filesystem implementation of the public corpus storage boundary.

Existing catalogs and CAS objects remain authoritative. Writers
share catalog-level locks; opening a store never migrates or rewrites records.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kernelcat_knowledge.contracts import ContractError, validate_source_record
from kernelcat_knowledge.corpus import CorpusError
from kernelcat_knowledge.ports import RepositoryError
from kernelcat_knowledge.ports.errors import ApplicationError
from kernelcat_knowledge.resources.binding import ResourceBinding
from kernelcat_knowledge.resources.configuration import RepositoryConfigError
from kernelcat_knowledge.sources.registration import (
    list_sources,
    register_source,
    show_source,
    verify_source,
)
from kernelcat_knowledge.storage.artifacts import FilesystemArtifactResolver
from kernelcat_knowledge.storage.catalog_io import (
    catalog_lock,
    check_directory,
    install,
    read_bytes,
)
from kernelcat_knowledge.storage.filesystem import EvidenceCatalog
from kernelcat_knowledge.storage.sources import FilesystemSourceRepository

_MARKER = ".kernelcat-evidence-root.json"
_MARKER_BYTES = b'{"schema_version":"kernelcat.evidence-root/v1"}'


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except CorpusError:
        raise
    except (
        ApplicationError,
        RepositoryError,
        ContractError,
        OSError,
        ValueError,
        RecursionError,
        RepositoryConfigError,
    ) as exc:
        raise CorpusError(str(exc)) from exc


def initialize(root: Path) -> None:
    with _errors():
        root = Path(os.path.abspath(root))
        check_directory(root)
        marker = root / _MARKER
        if root.exists() and any(root.iterdir()) and not marker.is_file():
            raise CorpusError("refusing to initialize an unrelated nonempty evidence root")
        install(marker, _MARKER_BYTES)


def _root(root: Path) -> Path:
    with _errors():
        root = Path(os.path.abspath(root))
        check_directory(root)
        try:
            marker = read_bytes(root / _MARKER)
        except FileNotFoundError:
            try:
                read_bytes(root / "config.yaml")
            except FileNotFoundError:
                raise CorpusError("Wiki evidence root missing; initialize it explicitly") from None
        else:
            if marker != _MARKER_BYTES:
                raise CorpusError("invalid evidence root marker")
        return root


class FilesystemEvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = _root(root)
        with _errors():
            self.binding = ResourceBinding.compose(self.root)
        self._source_root = self.binding.paths.source_records_dir
        self._cas_root = self.binding.paths.evidence_cas_dir
        with _errors():
            for path in (
                self._source_root,
                self.root / "data/evidence-catalog",
                self._cas_root,
            ):
                check_directory(path)
        self._sources = FilesystemSourceRepository(self._source_root)
        self._evidence = EvidenceCatalog(self.root / "data/evidence-catalog")
        self._resolver = FilesystemArtifactResolver(
            repo_root=self.root, cas_root=self._cas_root
        )

    def capture(self, identity: Mapping[str, Any], content: bytes) -> dict[str, Any]:
        with _errors():
            self.binding.require_sealable()
        identity = dict(identity)
        hashed = "sha256:" + hashlib.sha256(content).hexdigest()
        if (
            identity.get("digest") != hashed
            or identity.get("artifact_ref") != "cas://sha256/" + hashed[7:]
        ):
            raise CorpusError("source identity does not match captured bytes")
        if identity.get("status") != "active" or "retrieved_at" in identity:
            raise CorpusError(
                "capture requires an active identity without retrieved_at"
            )
        with _errors(), catalog_lock(self._source_root):
            record = validate_source_record(
                {**identity, "retrieved_at": datetime.now(UTC).isoformat()}
            )
            source_id = record["source_id"]
            old = self._sources.read(source_id)
            if old is not None:
                old = show_source(source_id, repository=self._sources)
                if any(old.get(key) != value for key, value in identity.items()):
                    raise CorpusError("source identity conflict or source is inactive")
                record["retrieved_at"] = old["retrieved_at"]
            raw = self._cas_root / "sha256" / hashed[7:9] / hashed[7:]
            install(raw, content)
            result = register_source(record, repository=self._sources)
            if result["status"] not in {"registered", "duplicate"}:
                raise CorpusError(
                    "Wiki source registration failed: " + str(result.get("code"))
                )
            return dict(show_source(source_id, repository=self._sources))

    def source(self, source_id: str) -> dict[str, Any]:
        with _errors():
            return dict(show_source(source_id, repository=self._sources))

    def list_sources(self) -> list[dict[str, Any]]:
        with _errors():
            return list_sources(repository=self._sources)["sources"]

    def verify_source(self, source_id: str) -> dict[str, Any]:
        with _errors():
            return verify_source(source_id, repository=self._sources, resolver=self._resolver)

    def read_source(self, source_id: str) -> bytes:
        source = self.source(source_id)
        if source["status"] != "active":
            raise CorpusError("inactive source")
        with _errors():
            proof = verify_source(source_id, repository=self._sources, resolver=self._resolver)
            if proof.get("content_verified") is not True:
                raise CorpusError("Wiki source bytes could not be verified")
            content = self._resolver.read_bytes(source["artifact_ref"])
        if "sha256:" + hashlib.sha256(content).hexdigest() != source["digest"]:
            raise CorpusError("source bytes changed during verification")
        return content

    def register_evidence(self, record: Mapping[str, Any]) -> None:
        with _errors():
            self.binding.require_sealable()
            result = self._evidence.register(record)
        if result["status"] not in {"registered", "duplicate"}:
            raise CorpusError("Wiki evidence registration failed")

    def evidence(self, evidence_id: str) -> dict[str, Any] | None:
        with _errors():
            row = self._evidence.get(evidence_id, strict=True)
            return dict(row) if row is not None else None

    def list_evidence(self) -> list[dict[str, Any]]:
        with _errors():
            return [dict(row) for row in self._evidence.list(strict=True)]
