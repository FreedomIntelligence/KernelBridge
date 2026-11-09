"""Atomic filesystem implementation of the TrajectoryRepository port."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kernelcat_knowledge.ports import CorruptRecordError, RepositoryError


class FilesystemTrajectoryRepository:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _target(self, delta_id: str) -> Path:
        return self.root / f"{delta_id}.json"

    def read(self, delta_id: str) -> Mapping[str, Any] | None:
        target = self._target(delta_id)
        if not target.exists():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptRecordError(str(exc)) from exc
        if not isinstance(payload, Mapping):
            raise CorruptRecordError("existing trajectory delta must be a JSON object")
        return payload

    def write_atomic(self, delta_id: str, canonical_payload: str) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RepositoryError(str(exc)) from exc
        target = self._target(delta_id)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(canonical_payload, encoding="utf-8")
            os.replace(temp, target)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RepositoryError(str(exc)) from exc
