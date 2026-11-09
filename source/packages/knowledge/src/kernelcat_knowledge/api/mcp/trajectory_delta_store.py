"""Compatibility wrapper over the shared trajectory outbox adapter."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kernelcat_knowledge.storage.filesystem import TrajectoryOutboxStore


def append_trajectory_delta(
    delta: Mapping[str, Any],
    *,
    outbox_dir: Path,
) -> dict[str, Any]:
    return TrajectoryOutboxStore(outbox_dir).append(delta)


__all__ = ["append_trajectory_delta"]
