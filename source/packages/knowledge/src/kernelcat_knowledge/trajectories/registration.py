"""Idempotent TrajectoryDelta semantics independent of filesystem transport."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    validate_trajectory_delta,
)
from kernelcat_knowledge.ports import (
    CorruptRecordError,
    RepositoryError,
    TrajectoryRepository,
)

_SAFE_ID = re.compile(r"^td_[A-Za-z0-9_-]{1,120}$")


class BundleIngestor(Protocol):
    def ingest(self, delta: Mapping[str, Any]) -> dict[str, Any]: ...


def append_trajectory_delta(
    delta: Mapping[str, Any],
    *,
    repository: TrajectoryRepository,
    bundle_ingestor: BundleIngestor | None = None,
) -> dict[str, Any]:
    """Validate, deduplicate and append one delta through repository ports.

    Bundle ingestion is deliberately fail-open after durable delta persistence.
    A bad or temporarily unavailable bundle returns ``ingest_status=degraded``
    plus a report/review item, but never drops the canonical TrajectoryDelta.
    """
    try:
        normalized = validate_trajectory_delta(delta)
    except ContractError as exc:
        return {"status": "error", "code": "INVALID_CONTRACT", "message": str(exc)}

    delta_id = str(normalized["trajectory_delta_id"])
    if not _SAFE_ID.fullmatch(delta_id):
        return {
            "status": "error",
            "code": "INVALID_ID",
            "message": "trajectory_delta_id contains unsupported characters",
        }

    try:
        existing = repository.read(delta_id)
    except CorruptRecordError as exc:
        return {
            "status": "error",
            "code": "CORRUPT_EXISTING_DELTA",
            "message": str(exc),
            "trajectory_delta_id": delta_id,
        }
    except RepositoryError as exc:
        return {"status": "error", "code": "READ_FAILED", "message": str(exc)}

    status = "appended"
    if existing is not None:
        try:
            existing_normalized = validate_trajectory_delta(existing)
        except ContractError as exc:
            return {
                "status": "error",
                "code": "CORRUPT_EXISTING_DELTA",
                "message": str(exc),
                "trajectory_delta_id": delta_id,
            }
        if existing_normalized["digest"] != normalized["digest"]:
            return {
                "status": "error",
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "the same trajectory_delta_id was already used for different contents",
                "trajectory_delta_id": delta_id,
            }
        status = "duplicate"
    else:
        try:
            repository.write_atomic(delta_id, canonical_json(normalized) + "\n")
        except RepositoryError as exc:
            return {"status": "error", "code": "WRITE_FAILED", "message": str(exc)}

    ingest_report: dict[str, Any] | None = None
    if bundle_ingestor is not None:
        try:
            ingest_report = bundle_ingestor.ingest(normalized)
        except Exception as exc:  # noqa: BLE001 -- adapter bugs must not erase an already durable delta
            ingest_report = {
                "schema_version": "kernelcat.bundle-ingest-report/v1",
                "status": "degraded",
                "bundles": [],
                "review_refs": [],
                "errors": [
                    {
                        "code": "BUNDLE_INGEST_INTERNAL",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                ],
            }

    result = {
        "status": status,
        "trajectory_delta_id": delta_id,
        "digest": normalized["digest"],
        "outbox_ref": f"trajectory-delta://{delta_id}",
    }
    if status == "appended":
        result.update({"episode_id": normalized["episode_id"], "terminal": normalized["terminal"]})
    if ingest_report is not None:
        result["bundle_ingest"] = ingest_report
        result["ingest_status"] = ingest_report.get("status", "degraded")
    return result
