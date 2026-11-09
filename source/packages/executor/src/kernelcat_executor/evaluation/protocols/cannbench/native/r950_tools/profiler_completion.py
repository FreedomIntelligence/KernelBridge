#!/usr/bin/env python3
"""Local completion records and atomic publication helpers for profiler runs."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


RUN_SCHEMA_VERSION = "profiler-run/v2"
ATTEMPT_SCHEMA_VERSION = "profiler-attempt-event/v1"
TOURNAMENT_SCHEMA_VERSION = "profiler-tournament-summary/v1"
LEGACY_PAIR_SCHEMA_VERSION = "real-profiler-ab-run/v1"


@dataclass(frozen=True)
class StageResult:
    """A stage return value with an optional remote wall-clock observation."""

    value: Any = None
    remote_elapsed_s: float | None = None


class StageRecorder:
    """Append-only in-memory stage ledger using time.monotonic as authority."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: list[dict[str, Any]] = []

    def run(
        self,
        stage: str,
        function: Callable[[], Any],
        *,
        arm: str | None = None,
        case_id: int | None = None,
    ) -> Any:
        started_at = _utc_now()
        started = time.monotonic()
        status = "ok"
        error: dict[str, str] | None = None
        remote_elapsed_s: float | None = None
        try:
            result = function()
            if isinstance(result, StageResult):
                remote_elapsed_s = result.remote_elapsed_s
                return result.value
            return result
        except Exception as exc:
            status = "failed"
            remote_elapsed_s = _finite_float(getattr(exc, "remote_elapsed_s", None))
            error = {
                "type": type(exc).__name__,
                "message": str(exc)[:4000],
            }
            raise
        finally:
            elapsed_s = round(max(0.0, time.monotonic() - started), 6)
            event = {
                "schema_version": ATTEMPT_SCHEMA_VERSION,
                "event_id": _attempt_event_id(
                    self.run_id,
                    len(self.events) + 1,
                    stage,
                    arm,
                    case_id,
                ),
                "run_id": self.run_id,
                "kind": "profile_stage",
                "stage": stage,
                "status": status,
                "arm": arm,
                "case_id": case_id,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "elapsed_s": elapsed_s,
                "remote_elapsed_s": remote_elapsed_s,
                "error": error,
                "artifact_refs": [],
                "provenance": {
                    "runner": "r950_tools/profiler_run.py",
                    "local_clock": "time.monotonic",
                },
            }
            self.events.append(_drop_none(event))

    def skipped(
        self,
        stage: str,
        reason: str,
        *,
        arm: str | None = None,
        case_id: int | None = None,
    ) -> None:
        now = _utc_now()
        event = {
            "schema_version": ATTEMPT_SCHEMA_VERSION,
            "event_id": _attempt_event_id(
                self.run_id,
                len(self.events) + 1,
                stage,
                arm,
                case_id,
            ),
            "run_id": self.run_id,
            "kind": "profile_stage",
            "stage": stage,
            "status": "skipped",
            "arm": arm,
            "case_id": case_id,
            "started_at": now,
            "finished_at": now,
            "elapsed_s": 0.0,
            "error": {"type": "StageSkipped", "message": reason[:4000]},
            "artifact_refs": [],
            "provenance": {
                "runner": "r950_tools/profiler_run.py",
                "local_clock": "time.monotonic",
            },
        }
        self.events.append(_drop_none(event))

    def record_commit(self) -> None:
        """Record the prepared local commit before the atomic directory publish."""
        now = _utc_now()
        self.events.append(
            {
                "schema_version": ATTEMPT_SCHEMA_VERSION,
                "event_id": _attempt_event_id(
                    self.run_id, len(self.events) + 1, "COMMIT", None, None
                ),
                "run_id": self.run_id,
                "kind": "profile_stage",
                "stage": "COMMIT",
                "status": "ok",
                "started_at": now,
                "finished_at": now,
                "elapsed_s": 0.0,
                "artifact_refs": [],
                "provenance": {
                    "runner": "r950_tools/profiler_run.py",
                    "local_clock": "time.monotonic",
                    "meaning": "completion bundle prepared for atomic publish",
                },
            }
        )

    def stage_durations(self) -> list[dict[str, Any]]:
        return [
            _drop_none(
                {
                    "stage": item.get("stage"),
                    "arm": item.get("arm"),
                    "case_id": item.get("case_id"),
                    "elapsed_s": item.get("elapsed_s"),
                    "remote_elapsed_s": item.get("remote_elapsed_s"),
                    "status": item.get("status"),
                    "error": (item.get("error") or {}).get("message"),
                }
            )
            for item in self.events
        ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _attempt_event_id(
    run_id: str,
    ordinal: int,
    stage: str,
    arm: str | None,
    case_id: int | None,
) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "run_id": run_id,
                "ordinal": ordinal,
                "stage": stage,
                "arm": arm,
                "case_id": case_id,
            }
        )
    ).hexdigest()
    return "attempt_" + digest


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        temporary.unlink()
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for row in rows
    )
    _atomic_write_text(path, text)
