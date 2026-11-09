#!/usr/bin/env python3
"""Atomic diagnosis artifact publication for completed profiler runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import profiler_run_core as _core
from profiler_agent_diagnosis import (
    build_diagnosis_first_projection,
    serialize_agent_diagnosis,
)
from profiler_diagnosis import build_profiler_diagnosis, serialize_diagnosis
from profiler_evidence_identity import (
    build_code_attribution,
    build_route_identity,
    captured_metric_groups,
)
from profiler_run_support import METRIC_PLAN_SCHEMA


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _core._atomic_write_json(path, payload)


def _copy_if_file(source: Path, target: Path) -> None:
    if source.is_file():
        _core._atomic_copy(source, target)


def _attach_new_attempt_refs(root: Path, refs: list[str]) -> None:
    path = root / "attempts.jsonl"
    if not path.is_file():
        return
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if row.get("stage") == "PROJECT":
            existing = list(row.get("artifact_refs") or [])
            row["artifact_refs"] = list(dict.fromkeys([*existing, *refs]))
    _core._write_attempts(path, rows)


def enrich_completed_run(
    artifact_root: Path,
    *,
    args,
    metrics: tuple[str, ...],
    explicit_metrics: bool,
    surface: Mapping[str, Any],
) -> None:
    metadata_paths = sorted(artifact_root.glob("pairs/*/run_metadata.json"))
    if len(metadata_paths) != 1:
        raise RuntimeError(f"expected one pair metadata file, found {len(metadata_paths)}")
    metadata_path = metadata_paths[0]
    pair_dir = metadata_path.parent
    pair_rel = pair_dir.relative_to(artifact_root).as_posix()
    metadata = _read_json(metadata_path)
    source_hashes = metadata.get("source_tree_sha256") or {}

    code_ref = f"{pair_rel}/code_attribution.json"
    route_ref = f"{pair_rel}/route_identity.json"
    event_log_ref = f"{pair_rel}/event_mt/evaluation.log"
    code = build_code_attribution(
        Path(args.base_source),
        Path(args.candidate_source),
        source_hashes=source_hashes,
        action_cell=args.action_cell,
        artifact_ref=code_ref,
    )
    route = build_route_identity(
        pair_dir / "event_mt" / "evaluation.log",
        run_id=str(args.run_id),
        case_id=int(args.case_id),
        candidate_arm="mt",
        action_cell=args.action_cell,
        candidate_source_sha256=str(source_hashes.get("mt") or ""),
        candidate_source=Path(args.candidate_source),
        event_log_ref=event_log_ref,
        artifact_ref=route_ref,
    )
    _write_json(pair_dir / "code_attribution.json", code)
    _write_json(pair_dir / "route_identity.json", route)

    captured = captured_metric_groups(pair_dir)
    missing = [metric for metric in metrics if metric not in captured]
    metadata["metric"] = metrics[0]
    metadata["pmu_metric_groups"] = {
        "schema_version": METRIC_PLAN_SCHEMA,
        "mode": "explicit" if explicit_metrics else "default_diagnostic_pack",
        "requested": list(metrics),
        "captured": captured,
        "missing": missing,
        "one_group_per_profiler_invocation": True,
    }
    metadata["route_identity"] = route
    metadata["code_attribution"] = code
    metadata["diagnosis_entrypoint"] = "diagnosis.json"
    metadata["legacy_agent_feedback_entrypoint"] = "agent_feedback.json"
    metadata["agent_feedback_entrypoint"] = "agent_diagnosis.json"
    execution = metadata.setdefault("execution", {})
    execution["identity_producer"] = "profiler_evidence_identity.py"
    execution["route_marker_contract"] = (
        "KERNELCAT_ROUTE_EVIDENCE + kernelcat.route-observation/v1"
    )
    _write_json(metadata_path, metadata)

    query_completed_run = surface["query_completed_run"]
    serialize_feedback = surface["serialize_feedback"]
    build_agent_projection = surface["build_agent_projection"]
    serialize_projection = surface["serialize_projection"]

    feedback = query_completed_run([pair_dir], action_cell=args.action_cell)
    _core._atomic_write_text(pair_dir / "unified_feedback.json", serialize_feedback(feedback))

    legacy_projection_error: str | None = None
    try:
        legacy_projection = build_agent_projection(
            feedback, token_budget=int(args.agent_token_budget)
        )
        extraction = legacy_projection.get("extraction") or {}
        tokens = int(extraction.get("tokens") or 0)
        if extraction.get("budget_satisfied") is False or tokens > int(args.agent_token_budget):
            raise ValueError(
                f"legacy agent projection exceeded token budget: {tokens}/{args.agent_token_budget}"
            )
        _core._atomic_write_text(
            pair_dir / "agent_feedback.json", serialize_projection(legacy_projection)
        )
    except Exception as exc:
        legacy_projection_error = _core._error_text(exc)
        (pair_dir / "agent_feedback.json").unlink(missing_ok=True)

    diagnosis = build_profiler_diagnosis(feedback)
    _core._atomic_write_text(pair_dir / "diagnosis.json", serialize_diagnosis(diagnosis))
    diagnosis_agent = build_diagnosis_first_projection(
        diagnosis,
        token_budget=int(args.agent_token_budget),
        legacy_entrypoint=(
            "agent_feedback.json" if legacy_projection_error is None else "unified_feedback.json"
        ),
    )
    _core._atomic_write_text(
        pair_dir / "agent_diagnosis.json", serialize_agent_diagnosis(diagnosis_agent)
    )

    metadata = _read_json(metadata_path)
    metadata["agent_feedback_entrypoint"] = "agent_diagnosis.json"
    metadata["legacy_agent_feedback_entrypoint"] = (
        "agent_feedback.json" if legacy_projection_error is None else "unified_feedback.json"
    )
    if legacy_projection_error:
        (artifact_root / "agent_feedback.json").unlink(missing_ok=True)
        metadata.setdefault("execution", {})[
            "legacy_projection_caveat"
        ] = legacy_projection_error
    _write_json(metadata_path, metadata)

    for name in (
        "run_metadata.json",
        "unified_feedback.json",
        "agent_feedback.json",
        "agent_diagnosis.json",
        "diagnosis.json",
        "route_identity.json",
        "code_attribution.json",
    ):
        _copy_if_file(pair_dir / name, artifact_root / name)

    manifest_path = artifact_root / "run_manifest.json"
    manifest = _read_json(manifest_path)
    experiment_key = manifest.setdefault("experiment_key", {})
    experiment_key["pmu_metric_groups"] = list(metrics)
    experiment_key["pmu_collection_plan"] = metadata["pmu_metric_groups"]
    manifest["pmu_collection"] = metadata["pmu_metric_groups"]
    manifest["entrypoints"] = {
        "agent": "agent_diagnosis.json",
        "legacy_agent": (
            "agent_feedback.json" if legacy_projection_error is None else "unified_feedback.json"
        ),
        "diagnosis": "diagnosis.json",
        "audit": "unified_feedback.json",
        "route_identity": "route_identity.json",
        "code_attribution": "code_attribution.json",
    }
    manifest["diagnosis_closure"] = {
        "route_status": route.get("status"),
        "code_attribution_status": code.get("status"),
        "highest_attribution_level": (diagnosis.get("summary") or {}).get(
            "highest_attribution_level"
        ),
        "default_agent_view": "diagnosis_first",
        "legacy_agent_preserved": legacy_projection_error is None,
    }
    if missing:
        manifest.setdefault("caveats", []).append(
            {
                "code": "pmu_metric_groups_missing",
                "message": "one or more directed PMU captures failed or were unavailable",
                "groups": missing,
            }
        )
    if legacy_projection_error:
        manifest.setdefault("caveats", []).append(
            {"code": "legacy_agent_projection_failed", "message": legacy_projection_error}
        )
    _write_json(manifest_path, manifest)

    _attach_new_attempt_refs(
        artifact_root,
        [
            f"{pair_rel}/agent_diagnosis.json",
            f"{pair_rel}/diagnosis.json",
            f"{pair_rel}/route_identity.json",
            f"{pair_rel}/code_attribution.json",
        ],
    )


def publish_enrichment_failure(artifact_root: Path, error: BaseException) -> None:
    manifest_path = artifact_root / "run_manifest.json"
    if not manifest_path.is_file():
        return
    manifest = _read_json(manifest_path)
    manifest.setdefault("caveats", []).append(
        {"code": "diagnosis_closure_failed", "message": _core._error_text(error)}
    )
    manifest["diagnosis_closure"] = {
        "status": "failed",
        "fail_closed": True,
        "default_agent_view": manifest.get("entrypoints", {}).get("agent"),
    }
    _write_json(manifest_path, manifest)
