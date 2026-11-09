"""Protocol-independent ports used by the Wiki application layer."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol


class KnowledgeRepository(Protocol):
    active_index_revision: str
    load_errors: list[str]

    def load_all(self) -> list[dict[str, Any]]: ...
    def load_all_revisions(self) -> list[dict[str, Any]]: ...
    def load_tier(self, tier: str) -> list[dict[str, Any]]: ...
    def find_by_id(self, knowledge_id: str) -> dict[str, Any] | None: ...
    def find_by_path(self, path: str | Path) -> dict[str, Any] | None: ...


class SourceRepository(Protocol):
    def register(self, record: Mapping[str, Any]) -> dict[str, Any]: ...
    def get(self, source_id: str) -> dict[str, Any] | None: ...
    def list(self, *, source_class: str | None = None) -> list[dict[str, Any]]: ...
    def verify(self, source_id: str) -> dict[str, Any]: ...


class EvidenceRepository(Protocol):
    def register(self, evidence: Mapping[str, Any]) -> dict[str, Any]: ...
    def get(self, evidence_id: str) -> dict[str, Any] | None: ...
    def list(self) -> list[dict[str, Any]]: ...


class TrajectoryOutbox(Protocol):
    def append(self, delta: Mapping[str, Any]) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...


class ReadModel(Protocol):
    path: Path

    def rebuild(
        self,
        cards: Sequence[Mapping[str, Any]],
        *,
        active_index_revision: str,
    ) -> dict[str, Any]: ...

    def status(self, *, expected_revision: str | None = None) -> dict[str, Any]: ...

    def search(
        self,
        text: str,
        *,
        candidate_ids: Sequence[str] | None = None,
        limit: int = 30,
        expected_revision: str | None = None,
    ) -> list[str]: ...
