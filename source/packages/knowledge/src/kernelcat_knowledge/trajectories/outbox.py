"""Idempotent trajectory outbox operations and status."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.ports import TrajectoryOutbox


def append_trajectory(
    delta: Mapping[str, Any], *, outbox: TrajectoryOutbox, legacy: bool = False
) -> dict[str, Any]:
    result = outbox.append(delta)
    if legacy:
        return result
    return response(
        "kernelcat.trajectory-operation/v1",
        str(result.get("status") or "error"),
        **{
            key: value
            for key, value in result.items()
            if key not in {"status", "digest"}
        },
    )


def outbox_status(*, outbox: TrajectoryOutbox) -> dict[str, Any]:
    result = outbox.status()
    return response(
        "kernelcat.trajectory-operation/v1",
        str(result.get("status") or "error"),
        **{
            key: value
            for key, value in result.items()
            if key not in {"status", "digest"}
        },
    )
