"""Public storage boundary for immutable source-backed data.

Callers own domain identities and interpretation. Wiki owns storage layout,
immutable installation, catalog locking and source-byte verification. Opening a
store never initializes a corpus or falls back to packaged knowledge.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol


class CorpusError(ValueError):
    """An unavailable corpus, conflicting identity or failed integrity check."""


class EvidenceStore(Protocol):
    def capture(self, identity: Mapping[str, Any], content: bytes) -> dict[str, Any]:
        """Install pinned source bytes and metadata; preserve first retrieved_at."""
        ...

    def source(self, source_id: str) -> dict[str, Any]: ...
    def list_sources(self) -> list[dict[str, Any]]: ...
    def verify_source(self, source_id: str) -> dict[str, Any]: ...
    def read_source(self, source_id: str) -> bytes: ...
    def register_evidence(self, record: Mapping[str, Any]) -> None: ...
    def evidence(self, evidence_id: str) -> dict[str, Any] | None: ...
    def list_evidence(self) -> list[dict[str, Any]]: ...


def initialize_corpus(root: Path) -> None:
    """Explicitly initialize a new external corpus, never adopt unrelated data."""
    from kernelcat_knowledge.storage.corpus import initialize

    initialize(root)


def open_evidence_store(root: Path) -> EvidenceStore:
    from kernelcat_knowledge.storage.corpus import FilesystemEvidenceStore

    return FilesystemEvidenceStore(root)
