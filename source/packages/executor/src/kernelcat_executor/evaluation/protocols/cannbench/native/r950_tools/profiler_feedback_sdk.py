#!/usr/bin/env python3
"""Query one or more completed profiler artifact trees exactly once.

The SDK joins three deliberately separate evidence layers:

* profiler-off NPU Event samples decide the latency A/B outcome;
* directed PMU captures explain a mechanism but never provide the reward;
* CANNBench final-eval JSON provides correctness and golden-result context.

Recognised artifacts are discovered recursively.  Unknown schemas, incomplete
protocol identities, and conflicting duplicate captures are rejected instead
of being silently paired.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from profiler_adoption import (
    ADOPTION_PROVENANCE_FIELDS,
    build_ab_profile_records,
    build_timing_ab_analysis,
    evaluate_adoption,
)
from profiler_feedback import _token_counter, compact_feedback, token_audit


SCHEMA_VERSION = "ascend-profiler-feedback-query/v1"
_REQUIRED_EVENT_METADATA = (
    "run_id",
    "operator",
    "arm",
    "case_id",
    "device",
    "warmup",
    "inner_repeat",
    "timing_scope",
    "cache_clean_each_sample",
    *ADOPTION_PROVENANCE_FIELDS,
)
_REQUIRED_PMU_METADATA = (
    "run_id",
    "operator",
    "arm",
    "case_id",
    "device",
    *ADOPTION_PROVENANCE_FIELDS,
)

# Expected directions are intentionally narrow.  A field with no configured
# rule is reported but does not vote on the mechanism verdict.
DEFAULT_EXPECTED_DIRECTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "async_pipeline.shared_row_prefetch": (
        (r"(?:^|\.)aiv_vec_ratio$", "increase"),
        (r"(?:^|\.)aiv_mte2_ratio$", "decrease"),
    ),
    "vectorized_memory": (
        (r"(?:^|\.)aiv_vec_ratio$", "increase"),
        (r"(?:^|\.)aiv_mte2_ratio$", "decrease"),
    ),
    "vectorized_datacopy": (
        (r"(?:^|\.)aiv_vec_ratio$", "increase"),
        (r"(?:^|\.)aiv_mte2_ratio$", "decrease"),
    ),
    "async_pipeline": (
        (r"(?:^|\.)wait_time_us$", "decrease"),
        (r"(?:^|\.)(?:aiv_vec_ratio|aic_cube_ratio)$", "increase"),
    ),
    "double_buffer": (
        (r"(?:^|\.)wait_time_us$", "decrease"),
        (r"(?:^|\.)(?:aiv_vec_ratio|aic_cube_ratio)$", "increase"),
    ),
}


def serialize_feedback(payload: Mapping[str, Any]) -> str:
    """Use the one canonical on-disk representation for output token audits."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class _Artifact:
    kind: str
    path: Path
    root: Path
    payload: dict[str, Any]
    ref_id: str
    sha256: str


def _number(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1].strip()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _case_index(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else None


def _operator(value: Any) -> str:
    return str(value or "").strip().casefold()


def _canonical(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.]+", "_", str(value or "")).strip("_")


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _metrics_shape_is_valid(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(field, str) and isinstance(stats, dict)
        for field, stats in value.items()
    )


def _artifact_kind(path: Path) -> str | None:
    name = path.name.casefold()
    if name == "event_samples.json":
        return "event"
    if name == "pipeprof.json":
        return "pmu"
    if fnmatch.fnmatch(name, "cann_final_eval*.json"):
        return "cannbench_final"
    if name == "run_metadata.json":
        return "run_metadata"
    return None


def _rejection(
    code: str, message: str, refs: Iterable[str] = ()
) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    artifact_refs = sorted(set(refs))
    if artifact_refs:
        item["artifact_refs"] = artifact_refs
    return item


def _evidence_catalog(
    artifacts: list[_Artifact], rejections: list[dict[str, Any]]
) -> dict[str, Any]:
    """Embed every parsed JSON payload once while retaining all source refs."""
    rejected_refs = {
        ref
        for rejection in rejections
        for ref in rejection.get("artifact_refs") or []
    }
    groups: dict[tuple[str, bytes], list[_Artifact]] = {}
    for artifact in artifacts:
        groups.setdefault((artifact.kind, _canonical_json(artifact.payload)), []).append(
            artifact
        )
    entries = []
    for index, ((kind, canonical), group) in enumerate(
        sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])), start=1
    ):
        refs = sorted(artifact.ref_id for artifact in group)
        family = _capture_family(group[0].path)
        if kind == "event":
            role = "authoritative profiler-off timing evidence"
        elif kind == "pmu":
            role = "raw mechanism evidence; all profiler timing is perturbed and never reward"
        elif kind == "cannbench_final" and family == "pmu":
            role = "raw profiled-run audit; correctness may be inspected, timing is never reward"
        elif kind == "cannbench_final":
            role = "CANNBench correctness and golden-result context"
        else:
            role = "run identity, source hashes, protocol, and precheck provenance"
        entries.append(
            {
                "id": f"e{index:04d}",
                "kind": kind,
                "artifact_refs": refs,
                "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
                "role": role,
                "validation": (
                    "rejected" if any(ref in rejected_refs for ref in refs) else "accepted"
                ),
                "payload": group[0].payload,
            }
        )
    return {
        "mode": "self-contained parsed JSON; semantic duplicates share one payload",
        "source_payloads": len(artifacts),
        "unique_payloads": len(entries),
        "entries": entries,
    }


def _discover(
    artifact_roots: Iterable[str | Path],
) -> tuple[list[_Artifact], list[dict[str, Any]], list[dict[str, str]]]:
    roots: list[Path] = []
    rejections: list[dict[str, Any]] = []
    candidates: dict[str, tuple[Path, Path, str]] = {}
    for raw_root in artifact_roots:
        root = Path(raw_root).expanduser().resolve()
        roots.append(root)
        if not root.exists():
            rejections.append(
                _rejection("artifact_root_missing", f"artifact root does not exist: {root}")
            )
            continue
        paths = [root] if root.is_file() else root.rglob("*.json")
        for path in paths:
            kind = _artifact_kind(path)
            if kind is None:
                continue
            resolved = path.resolve()
            candidates.setdefault(str(resolved).casefold(), (resolved, root, kind))

    artifacts: list[_Artifact] = []
    refs: list[dict[str, str]] = []
    for index, (_, (path, root, kind)) in enumerate(sorted(candidates.items()), start=1):
        ref_id = f"a{index:04d}"
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            rejections.append(
                _rejection("artifact_parse_error", f"cannot parse {path}: {exc}", [ref_id])
            )
            refs.append(
                {"id": ref_id, "kind": kind, "path": str(path), "root": str(root)}
            )
            continue
        if not isinstance(payload, dict):
            rejections.append(
                _rejection(
                    "unexpected_schema",
                    f"recognised artifact must contain a JSON object: {path}",
                    [ref_id],
                )
            )
            refs.append(
                {"id": ref_id, "kind": kind, "path": str(path), "root": str(root)}
            )
            continue
        digest = hashlib.sha256(raw).hexdigest()
        artifacts.append(_Artifact(kind, path, root, payload, ref_id, digest))
        refs.append(
            {
                "id": ref_id,
                "kind": kind,
                "path": str(path),
                "root": str(root),
                "sha256": digest,
            }
        )
    return artifacts, rejections, refs


def _validate_schema(
    artifacts: list[_Artifact], rejections: list[dict[str, Any]]
) -> list[_Artifact]:
    accepted = []
    for artifact in artifacts:
        payload = json.loads(json.dumps(artifact.payload))
        metadata = payload.get("metadata")
        reason: str | None = None
        if artifact.kind == "event":
            # Older v1 producer builds predate explicit protocol fields.  The
            # only possible semantics were one call per sample, so normalise
            # that losslessly instead of discarding an otherwise valid run.
            if isinstance(metadata, dict):
                metadata.setdefault("inner_repeat", 1)
                metadata.setdefault("timing_scope", "single_call")
            if (
                payload.get("schema_version") != "profiler-off-timing/v1"
                or payload.get("profiler_enabled") is not False
                or payload.get("timer") != "npu_event"
                or payload.get("unit") != "us"
            ):
                reason = "event_samples.json is not profiler-off NPU Event timing v1"
            elif not isinstance(metadata, dict) or any(
                field not in metadata or metadata.get(field) in (None, "")
                for field in _REQUIRED_EVENT_METADATA
            ):
                reason = "event timing has an incomplete run/op/case/device/protocol identity"
            elif _case_index(metadata.get("case_id")) is None:
                reason = "event timing case_id is not parseable"
            elif type(metadata.get("warmup")) is not int or metadata["warmup"] < 0:
                reason = "event timing warmup must be a non-negative integer"
            elif (
                type(metadata.get("inner_repeat")) is not int
                or not 1 <= metadata["inner_repeat"] <= 1000
            ):
                reason = "event timing inner_repeat must be an integer in [1, 1000]"
            elif type(metadata.get("cache_clean_each_sample")) is not bool:
                reason = "event timing cache_clean_each_sample must be boolean"
            elif metadata.get("timing_scope") not in {
                "single_call",
                "batched_steady_state_per_call",
                "batched_cold_start_amortized_per_call",
            }:
                reason = "event timing_scope is unknown"
            else:
                samples = payload.get("samples_us")
                if not isinstance(samples, list) or not samples:
                    reason = "event timing has no samples_us array"
                elif any(
                    isinstance(value, bool)
                    or (number := _number(value)) is None
                    or number <= 0
                    for value in samples
                ):
                    reason = "event timing samples_us must contain finite positive numbers"
        elif artifact.kind == "pmu":
            if (
                payload.get("schema_version") != "pipeprof/v1"
                or payload.get("capture_kind") != "pmu"
            ):
                reason = "pipeprof.json is not a directed PMU capture v1"
            elif not isinstance(metadata, dict):
                reason = "PMU metadata must be an object"
            elif metadata.get("patch_verified") is not True:
                reason = "pipeprof PMU monkeypatch was not verified"
            elif any(
                field not in metadata or metadata.get(field) in (None, "")
                for field in _REQUIRED_PMU_METADATA
            ):
                reason = (
                    "PMU capture has an incomplete run/op/arm/case/device/provenance identity"
                )
            elif _case_index(metadata.get("case_id")) is None:
                reason = "PMU case_id is not parseable"
            elif not str(payload.get("metric_group") or "").strip():
                reason = "PMU metric_group is missing"
            elif type(payload.get("available")) is not bool:
                reason = "PMU available must be boolean"
            elif type(payload.get("kernel_count")) is not int or payload["kernel_count"] < 0:
                reason = "PMU kernel_count must be a non-negative integer"
            elif any(
                not _metrics_shape_is_valid(payload.get(field) or {})
                for field in (
                    "duration_weighted_metrics",
                    "aggregate_metrics",
                    "context_metrics",
                )
            ):
                reason = "PMU metric sections must be objects of statistic objects"
        elif artifact.kind == "cannbench_final":
            operators = payload.get("operators")
            if not isinstance(operators, list):
                reason = "CANNBench final-eval JSON has no operators array"
            elif any(
                not isinstance(operator, dict)
                or not str(operator.get("operator") or "").strip()
                or not isinstance(operator.get("cases"), list)
                or any(
                    not isinstance(case, dict)
                    or _case_index(case.get("case_id")) is None
                    or (
                        case.get("accuracy") is not None
                        and not isinstance(case.get("accuracy"), dict)
                    )
                    or (
                        isinstance(case.get("accuracy"), dict)
                        and case["accuracy"].get("metadata") is not None
                        and not isinstance(case["accuracy"].get("metadata"), dict)
                    )
                    or (
                        case.get("op_times") is not None
                        and not isinstance(case.get("op_times"), dict)
                    )
                    or (
                        isinstance(case.get("op_times"), dict)
                        and case["op_times"].get("device_kernels") is not None
                        and not isinstance(
                            case["op_times"].get("device_kernels"), dict
                        )
                    )
                    for case in operator.get("cases")
                )
                for operator in operators
            ):
                reason = "CANNBench final-eval operator/case structure is invalid"
        else:
            source_hashes = payload.get("source_tree_sha256")
            event_timing = payload.get("event_timing")
            if payload.get("schema_version") != "real-profiler-ab-run/v1":
                reason = "run_metadata.json has an unknown schema"
            elif any(
                payload.get(field) in (None, "")
                for field in ("run_id", "device", "case_id", "action_cell")
            ):
                reason = "run metadata has an incomplete run/device/case/action identity"
            elif _case_index(payload.get("case_id")) is None:
                reason = "run metadata case_id is not parseable"
            elif not isinstance(event_timing, dict):
                reason = "run metadata event_timing must be an object"
            elif (
                type(event_timing.get("outer_samples")) is not int
                or event_timing["outer_samples"] <= 0
                or type(event_timing.get("inner_repeat")) is not int
                or not 1 <= event_timing["inner_repeat"] <= 1000
                or event_timing.get("cache_mode") not in {"cannbench", "steady"}
            ):
                reason = "run metadata event_timing protocol is invalid"
            elif not isinstance(source_hashes, dict) or any(
                not re.fullmatch(r"[0-9a-fA-F]{64}", str(source_hashes.get(arm) or ""))
                for arm in ("base", "mt")
            ):
                reason = "run metadata requires base/mt SHA-256 source identities"
        if reason:
            rejections.append(
                _rejection("unexpected_schema", reason, [artifact.ref_id])
            )
        else:
            accepted.append(
                _Artifact(
                    artifact.kind,
                    artifact.path,
                    artifact.root,
                    payload,
                    artifact.ref_id,
                    artifact.sha256,
                )
            )
    return accepted


def _provenance(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(metadata.get(field) or "").strip() for field in ADOPTION_PROVENANCE_FIELDS
    )


def _event_identity(artifact: _Artifact) -> tuple[Any, ...]:
    metadata = artifact.payload["metadata"]
    return (
        str(metadata["run_id"]),
        _operator(metadata["operator"]),
        str(metadata["arm"]),
        _case_index(metadata["case_id"]),
        str(metadata["device"]),
        metadata.get("warmup"),
        int(metadata["inner_repeat"]),
        str(metadata["timing_scope"]),
        bool(metadata["cache_clean_each_sample"]),
        _provenance(metadata),
    )


def _pmu_identity(artifact: _Artifact) -> tuple[Any, ...]:
    payload = artifact.payload
    metadata = payload["metadata"]
    return (
        str(metadata["run_id"]),
        _operator(metadata["operator"]),
        str(metadata["arm"]),
        _case_index(metadata["case_id"]),
        str(metadata["device"]),
        str(payload.get("metric_group") or "unknown"),
        _provenance(metadata),
    )


def _deduplicate_captures(
    artifacts: list[_Artifact], rejections: list[dict[str, Any]]
) -> tuple[list[_Artifact], int]:
    kept: list[_Artifact] = []
    duplicates_ignored = 0
    for kind, identity_fn in (("event", _event_identity), ("pmu", _pmu_identity)):
        groups: dict[tuple[Any, ...], list[_Artifact]] = {}
        for artifact in artifacts:
            if artifact.kind == kind:
                groups.setdefault(identity_fn(artifact), []).append(artifact)
        for identity, captures in sorted(groups.items(), key=lambda item: repr(item[0])):
            canonical = {_canonical_json(item.payload) for item in captures}
            if len(canonical) > 1:
                rejections.append(
                    _rejection(
                        f"conflicting_{kind}_capture",
                        f"conflicting duplicate {kind} capture for identity {identity!r}",
                        [item.ref_id for item in captures],
                    )
                )
                continue
            kept.append(captures[0])
            duplicates_ignored += len(captures) - 1
    kept.extend(
        artifact
        for artifact in artifacts
        if artifact.kind in {"cannbench_final", "run_metadata"}
    )
    return sorted(kept, key=lambda item: str(item.path).casefold()), duplicates_ignored


def _event_context(artifact: _Artifact) -> tuple[Any, ...]:
    metadata = artifact.payload["metadata"]
    return (
        _operator(metadata["operator"]),
        str(metadata["run_id"]),
        _provenance(metadata),
        str(metadata["device"]),
        metadata.get("warmup"),
        int(metadata["inner_repeat"]),
        str(metadata["timing_scope"]),
        bool(metadata["cache_clean_each_sample"]),
    )


def _safe_pmu_values(report: dict[str, Any], stat: str) -> dict[str, float]:
    group = _canonical(report.get("metric_group") or "unknown")
    weighted = report.get("duration_weighted_metrics") or {}
    if weighted:
        metric_items = weighted.items()
        value_name = "duration_weighted_mean"
    elif report.get("kernel_count") == 1:
        metric_items = (report.get("aggregate_metrics") or {}).items()
        value_name = stat
    else:
        metric_items = ()
        value_name = stat
    values: dict[str, float] = {}
    for field, stats in metric_items:
        normalized = _canonical(field).casefold()
        if normalized in {"duration", "duration_us", "task_duration", "kernel_duration"}:
            continue
        value = _number((stats or {}).get(value_name))
        if value is not None:
            values[f"{group}.{_canonical(field)}"] = value

    for field, stats in (report.get("context_metrics") or {}).items():
        normalized = re.sub(r"[^a-z0-9]+", "_", str(field).casefold()).strip("_")
        canonical = {
            "wait_time": "wait_time_us",
            "wait_time_us": "wait_time_us",
            "block_num": "block_num",
            "mix_block_num": "mix_block_num",
        }.get(normalized)
        if canonical is None:
            continue
        value = _number((stats or {}).get(stat))
        if value is not None:
            values[f"scheduler.{canonical}"] = value
    return values


def _pmu_index(
    artifacts: list[_Artifact], rejections: list[dict[str, Any]], stat: str
) -> tuple[
    dict[tuple[str, str, str, int, str, tuple[str, ...]], dict[str, float]],
    dict[tuple[str, str, str, int, str, tuple[str, ...]], dict[str, str]],
]:
    result: dict[
        tuple[str, str, str, int, str, tuple[str, ...]], dict[str, float]
    ] = {}
    sources: dict[
        tuple[str, str, str, int, str, tuple[str, ...]], dict[str, str]
    ] = {}
    conflicted_fields: dict[
        tuple[str, str, str, int, str, tuple[str, ...]], set[str]
    ] = {}
    for artifact in artifacts:
        if artifact.kind != "pmu":
            continue
        metadata = artifact.payload["metadata"]
        key = (
            str(metadata["run_id"]),
            _operator(metadata["operator"]),
            str(metadata["arm"]),
            int(_case_index(metadata["case_id"])),
            str(metadata["device"]),
            _provenance(metadata),
        )
        bucket = result.setdefault(key, {})
        field_sources = sources.setdefault(key, {})
        tombstones = conflicted_fields.setdefault(key, set())
        for field, value in _safe_pmu_values(artifact.payload, stat).items():
            if field in tombstones:
                continue
            if field in bucket and not math.isclose(
                bucket[field], value, rel_tol=0.0, abs_tol=1e-12
            ):
                rejections.append(
                    _rejection(
                        "conflicting_pmu_field",
                        f"conflicting PMU value for {key!r} field {field}",
                        [field_sources[field], artifact.ref_id],
                    )
                )
                bucket.pop(field, None)
                field_sources.pop(field, None)
                tombstones.add(field)
                continue
            bucket[field] = value
            field_sources[field] = artifact.ref_id
    return result, sources


def _direction_verdict(
    action_cell: str,
    base: Mapping[str, float],
    candidate: Mapping[str, float],
    relative_delta: Mapping[str, float],
    expected_directions: Mapping[str, str] | None,
    *,
    stable: bool,
) -> dict[str, Any]:
    if expected_directions is None:
        action_key = action_cell.casefold()
        rules = DEFAULT_EXPECTED_DIRECTIONS.get(action_key)
        if rules is None:
            rules = next(
                (
                    configured
                    for prefix, configured in sorted(
                        DEFAULT_EXPECTED_DIRECTIONS.items(),
                        key=lambda item: len(item[0]),
                        reverse=True,
                    )
                    if action_key.startswith(prefix + ".")
                ),
                (),
            )
    else:
        rules = tuple(expected_directions.items())
    checks = []
    for field in sorted(set(base) & set(candidate)):
        expectation = next(
            (direction for pattern, direction in rules if re.search(pattern, field)), None
        )
        if expectation not in {"increase", "decrease"}:
            continue
        delta = candidate[field] - base[field]
        tolerance = max(abs(base[field]), abs(candidate[field]), 1.0) * 1e-12
        observed = "flat" if abs(delta) <= tolerance else ("increase" if delta > 0 else "decrease")
        checks.append(
            {
                "field": field,
                "expected": expectation,
                "observed": observed,
                "matches": observed == expectation,
                "relative_delta": relative_delta.get(field),
            }
        )
    if not rules:
        verdict = "not_configured"
    elif not checks:
        verdict = "no_comparable_expected_fields"
    elif not stable:
        verdict = "withheld_unstable_timing"
    elif all(item["matches"] for item in checks):
        verdict = "supported"
    elif any(item["matches"] for item in checks):
        verdict = "mixed"
    else:
        verdict = "not_supported"
    return {
        "action_cell": action_cell,
        "verdict": verdict,
        "checks": checks,
        "note": "counter direction is mechanism attribution, never the latency reward",
    }


def _attach_pair_ids(analysis: dict[str, Any], start: int) -> tuple[dict[str, Any], int]:
    copied = json.loads(json.dumps(analysis))
    next_id = start
    for arm, arm_result in sorted((copied.get("diff") or {}).items()):
        for row in arm_result.get("rows") or []:
            row["pair_id"] = f"t{next_id:04d}"
            row["candidate_arm"] = arm
            next_id += 1
    return copied, next_id


def _analysis_rows(analysis: dict[str, Any]):
    for arm, arm_result in sorted((analysis.get("diff") or {}).items()):
        for row in arm_result.get("rows") or []:
            yield arm, row


def _timing_pair_preference(
    analysis: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[int, int, int, int, int]:
    """Rank timing evidence without using its latency as a profiler reward."""
    capture = analysis.get("capture_metadata") or {}
    sample_counts = capture.get("sample_counts") or {}
    return (
        int(row.get("accepted") is True),
        int(row.get("stable") is True),
        int(not bool(capture.get("cache_clean_each_sample"))),
        int(capture.get("inner_repeat") or 1),
        min((int(value) for value in sample_counts.values()), default=0),
    )


def _analysis_preference(
    analysis: Mapping[str, Any], arm: str, base_arm: str
) -> tuple[int, int, int, int, int]:
    rows = [
        row
        for candidate_arm, row in _analysis_rows(dict(analysis))
        if arm == base_arm or candidate_arm == arm
    ]
    return max(
        (_timing_pair_preference(analysis, row) for row in rows),
        default=(0, 0, 0, 0, 0),
    )


def _single_pair_analysis(
    analysis: Mapping[str, Any], arm: str, row: Mapping[str, Any]
) -> dict[str, Any]:
    """Copy an analysis while retaining exactly one candidate/case timing pair."""
    copied = json.loads(json.dumps(analysis))
    arm_payload = dict((copied.get("diff") or {}).get(arm) or {})
    arm_payload["rows"] = [json.loads(json.dumps(row))]
    copied["diff"] = {arm: arm_payload}
    return copied


def _capture_family(path: Path) -> str | None:
    for part in reversed(path.parts):
        match = re.fullmatch(r"(event|pmu)\d*[_-](.+)", part, flags=re.IGNORECASE)
        if match:
            return match.group(1).casefold()
    return None


def _infer_arm(path: Path) -> str | None:
    for part in reversed(path.parts):
        match = re.fullmatch(r"(?:event|pmu)\d*[_-](.+)", part, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _eval_cases(payload: dict[str, Any]) -> set[tuple[str, int]]:
    result = set()
    for operator in payload.get("operators") or []:
        name = _operator(operator.get("operator"))
        for case in operator.get("cases") or []:
            case_id = _case_index(case.get("case_id"))
            if name and case_id is not None:
                result.add((name, case_id))
    return result


def _selected_final_projection(
    payload: Mapping[str, Any], selectors: Iterable[tuple[str, int]]
) -> bytes:
    wanted = set(selectors)
    selected = []
    for operator in payload.get("operators") or []:
        operator_key = _operator(operator.get("operator"))
        for case in operator.get("cases") or []:
            selector = (operator_key, _case_index(case.get("case_id")))
            if selector in wanted:
                selected.append(
                    {
                        "operator": operator.get("operator"),
                        "case": case,
                    }
                )
    return _canonical_json(selected)


def _common_depth(left: Path, right: Path) -> int:
    count = 0
    for a, b in zip(left.parts, right.parts):
        if a.casefold() != b.casefold():
            break
        count += 1
    return count


def _correctness_summary(feedback_items: list[dict[str, Any]]) -> dict[str, Any]:
    arms = []
    total = passed = failed = unknown = 0
    for item in feedback_items:
        cases = item["feedback"].get("cases") or []
        arm_passed = sum((case.get("correctness") or {}).get("passed") is True for case in cases)
        arm_failed = sum((case.get("correctness") or {}).get("passed") is False for case in cases)
        arm_unknown = len(cases) - arm_passed - arm_failed
        arms.append(
            {
                "run_id": item["run_id"],
                "arm": item["arm"],
                "cases": len(cases),
                "passed": arm_passed,
                "failed": arm_failed,
                "unknown": arm_unknown,
                "all_passed": bool(cases) and arm_passed == len(cases),
                "artifact_refs": item["artifact_refs"],
            }
        )
        total += len(cases)
        passed += arm_passed
        failed += arm_failed
        unknown += arm_unknown
    return {
        "cases": total,
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "all_passed": bool(total) and passed == total,
        "arms": arms,
    }


def query_profiler_feedback(
    artifact_roots: Iterable[str | Path],
    *,
    action_cell: str,
    base_arm: str = "base",
    stat: str = "p50",
    expected_directions: Mapping[str, str] | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> dict[str, Any]:
    """Return one fail-closed, JSON-serialisable view of completed artifacts.

    This pure function is the integration point for ``real_profiler_ab`` after
    its single remote run has completed and artifacts have been collected.
    It performs no remote queries and never modifies an artifact.
    """
    roots = [Path(root).expanduser().resolve() for root in artifact_roots]
    if not action_cell or not action_cell.strip():
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "rejected",
            "rejections": [_rejection("missing_action_cell", "action_cell is required")],
            "limitations": [],
            "notices": [],
            "artifacts": {"roots": [str(root) for root in roots], "refs": []},
        }
    if stat not in {"p50", "p95", "p99"}:
        raise ValueError("stat must be p50, p95, or p99")

    artifacts, rejections, artifact_refs = _discover(roots)
    discovered_artifacts = list(artifacts)
    artifacts = _validate_schema(artifacts, rejections)
    artifacts, duplicates_ignored = _deduplicate_captures(artifacts, rejections)
    events = [artifact for artifact in artifacts if artifact.kind == "event"]
    pmus = [artifact for artifact in artifacts if artifact.kind == "pmu"]
    finals = [artifact for artifact in artifacts if artifact.kind == "cannbench_final"]
    run_metadata = [
        artifact for artifact in artifacts if artifact.kind == "run_metadata"
    ]
    limitations: list[dict[str, Any]] = []
    notices: list[dict[str, Any]] = []

    event_groups: dict[tuple[Any, ...], list[_Artifact]] = {}
    for artifact in events:
        event_groups.setdefault(_event_context(artifact), []).append(artifact)

    analyses_internal: list[tuple[dict[str, Any], list[_Artifact]]] = []
    pair_number = 1
    for context, group in sorted(event_groups.items(), key=lambda item: repr(item[0])):
        analysis = build_timing_ab_analysis(
            [artifact.payload for artifact in group],
            base_arm=base_arm,
            action_cell=action_cell,
            stat=stat,
        )
        if not analysis:
            rejections.append(
                _rejection(
                    "unpaired_event_protocol",
                    f"no base/candidate Event pair for exact protocol context {context!r}",
                    [artifact.ref_id for artifact in group],
                )
            )
            continue
        analysis, pair_number = _attach_pair_ids(analysis, pair_number)
        analysis["artifact_refs"] = [artifact.ref_id for artifact in group]
        analyses_internal.append((analysis, group))

    run_identity_groups: dict[tuple[str, str, int, str], list[_Artifact]] = {}
    for artifact in run_metadata:
        payload = artifact.payload
        identity = (
            str(payload["run_id"]),
            str(payload["device"]),
            int(_case_index(payload["case_id"])),
            str(payload["action_cell"]),
        )
        run_identity_groups.setdefault(identity, []).append(artifact)

    conflicting_source_run_ids: set[str] = set()
    for identity, group in sorted(run_identity_groups.items()):
        source_projections = {
            _canonical_json(
                {
                    "source_tree_sha256": artifact.payload.get("source_tree_sha256"),
                    "metric": artifact.payload.get("metric"),
                    "sequence": artifact.payload.get("sequence"),
                }
            )
            for artifact in group
        }
        if len(source_projections) > 1:
            conflicting_source_run_ids.add(identity[0])
            rejections.append(
                _rejection(
                    "conflicting_run_metadata",
                    "same run/device/case/action has conflicting source identity",
                    [artifact.ref_id for artifact in group],
                )
            )

    run_metadata_groups: dict[tuple[Any, ...], list[_Artifact]] = {}
    for artifact in run_metadata:
        payload = artifact.payload
        event_timing = payload["event_timing"]
        identity = (
            str(payload["run_id"]),
            str(payload["device"]),
            int(_case_index(payload["case_id"])),
            str(payload["action_cell"]),
            int(event_timing["outer_samples"]),
            int(event_timing["inner_repeat"]),
            str(event_timing["cache_mode"]),
        )
        run_metadata_groups.setdefault(identity, []).append(artifact)

    run_contexts = []
    superseded_run_metadata = 0
    for _, group in sorted(run_metadata_groups.items(), key=lambda item: repr(item[0])):
        if str(group[0].payload["run_id"]) in conflicting_source_run_ids:
            superseded_run_metadata += len(group) - 1
            continue
        projections = {
            _canonical_json(
                {
                    "source_tree_sha256": artifact.payload.get("source_tree_sha256"),
                    "metric": artifact.payload.get("metric"),
                    "sequence": artifact.payload.get("sequence"),
                }
            )
            for artifact in group
        }
        superseded_run_metadata += len(group) - 1
        if len(projections) != 1:  # defended by the run-wide check above
            continue
        artifact = min(group, key=lambda item: str(item.path).casefold())
        metadata_payload = artifact.payload
        run_id = str(metadata_payload["run_id"])
        matching = [
            analysis
            for analysis, _ in analyses_internal
            if str(analysis.get("run_id")) == run_id
        ]
        if str(metadata_payload.get("action_cell")) != action_cell:
            rejections.append(
                _rejection(
                    "run_metadata_action_mismatch",
                    f"run metadata action_cell does not match query: {artifact.path}",
                    [artifact.ref_id],
                )
            )
        if not matching:
            limitations.append(
                {
                    "code": "run_metadata_unmatched",
                    "message": f"run metadata has no matching Event analysis: {artifact.path}",
                    "artifact_refs": [artifact.ref_id],
                }
            )
        else:
            event_timing = metadata_payload["event_timing"]
            expected_cache_clean = (
                False if event_timing.get("cache_mode") == "steady" else None
            )
            protocol_match = any(
                str((analysis.get("capture_metadata") or {}).get("device"))
                == str(metadata_payload.get("device"))
                and int((analysis.get("capture_metadata") or {}).get("inner_repeat") or 0)
                == int(event_timing.get("inner_repeat") or 0)
                and (
                    expected_cache_clean is None
                    or bool(
                        (analysis.get("capture_metadata") or {}).get(
                            "cache_clean_each_sample"
                        )
                    )
                    == expected_cache_clean
                )
                and all(
                    int(count) == int(event_timing.get("outer_samples"))
                    for count in (
                        (analysis.get("capture_metadata") or {}).get("sample_counts")
                        or {}
                    ).values()
                )
                and any(
                    _case_index(row.get("idx"))
                    == _case_index(metadata_payload.get("case_id"))
                    for _, row in _analysis_rows(analysis)
                )
                for analysis in matching
            )
            if not protocol_match:
                rejections.append(
                    _rejection(
                        "run_metadata_protocol_mismatch",
                        f"run metadata does not match Event device/case/protocol: {artifact.path}",
                        [artifact.ref_id],
                    )
                )
        run_contexts.append(
            {
                "artifact_refs": sorted(item.ref_id for item in group),
                **metadata_payload,
            }
        )

    pmu_payloads = [artifact.payload for artifact in pmus]
    pmu_values, pmu_field_sources = _pmu_index(pmus, rejections, stat)
    pmu_pair_candidates: dict[
        tuple[str, str, int, str, str, tuple[str, ...]],
        list[tuple[dict[str, Any], dict[str, Any]]],
    ] = {}
    for analysis, _ in analyses_internal:
        run_id = str(analysis["run_id"])
        if run_id in conflicting_source_run_ids:
            analysis["source_identity_status"] = "conflicting; PMU/adoption withheld"
            continue
        op = _operator(analysis["op"])
        device = str((analysis.get("capture_metadata") or {}).get("device"))
        provenance = _provenance(analysis.get("provenance") or {})
        for arm, row in _analysis_rows(analysis):
            case_id = int(_case_index(row.get("idx")))
            pmu_pair_candidates.setdefault(
                (run_id, op, case_id, str(arm), device, provenance), []
            ).append((analysis, row))

    adoption_records: list[dict[str, Any]] = []
    pmu_comparisons = []
    for (run_id, op, case_id, arm, device, provenance), candidates in sorted(
        pmu_pair_candidates.items()
    ):
        analysis, row = max(
            candidates,
            key=lambda item: (
                _timing_pair_preference(item[0], item[1]),
                str(item[1].get("pair_id") or ""),
            ),
        )
        adoption_records.extend(
            build_ab_profile_records(
                _single_pair_analysis(analysis, arm, row),
                [
                    report
                    for report in pmu_payloads
                    if str((report.get("metadata") or {}).get("device")) == device
                ],
                stat=stat,
            )
        )
        timing_pair_refs = sorted(
            {str(candidate_row["pair_id"]) for _, candidate_row in candidates}
        )
        base_key = (run_id, op, base_arm, case_id, device, provenance)
        candidate_key = (run_id, op, arm, case_id, device, provenance)
        base_values = pmu_values.get(base_key, {})
        candidate_values = pmu_values.get(candidate_key, {})
        base_artifact_refs = sorted(set(pmu_field_sources.get(base_key, {}).values()))
        candidate_artifact_refs = sorted(
            set(pmu_field_sources.get(candidate_key, {}).values())
        )
        common = sorted(set(base_values) & set(candidate_values))
        relative = {
            field: (candidate_values[field] - base_values[field]) / abs(base_values[field])
            for field in common
            if abs(base_values[field]) > 1e-12
        }
        if not base_values or not candidate_values:
            limitations.append(
                {
                    "code": "pmu_pair_unavailable",
                    "message": (
                        f"no complete PMU base/candidate pair for "
                        f"{run_id}/{op}/case{case_id}/{arm}"
                    ),
                    "timing_pair_refs": timing_pair_refs,
                }
            )
            continue
        if not common:
            limitations.append(
                {
                    "code": "pmu_fields_not_comparable",
                    "message": (
                        f"PMU base/candidate captures have no common safe fields for "
                        f"{run_id}/{op}/case{case_id}/{arm}"
                    ),
                    "timing_pair_refs": timing_pair_refs,
                    "base_artifact_refs": base_artifact_refs,
                    "candidate_artifact_refs": candidate_artifact_refs,
                }
            )
            continue
        direction = _direction_verdict(
            action_cell,
            base_values,
            candidate_values,
            relative,
            expected_directions,
            stable=row.get("stable") is True,
        )
        if direction["verdict"] == "no_comparable_expected_fields":
            limitations.append(
                {
                    "code": "expected_pmu_fields_unavailable",
                    "message": (
                        f"PMU captures have scheduler context but no comparable expected "
                        f"counter for {run_id}/{op}/case{case_id}/{arm}"
                    ),
                    "base_artifact_refs": base_artifact_refs,
                    "candidate_artifact_refs": candidate_artifact_refs,
                }
            )
        pmu_comparisons.append(
            {
                "selected_timing_pair_ref": row["pair_id"],
                "timing_pair_refs": timing_pair_refs,
                "run_id": run_id,
                "operator": op,
                "case_id": case_id,
                "base_arm": base_arm,
                "candidate_arm": arm,
                "base_artifact_refs": base_artifact_refs,
                "candidate_artifact_refs": candidate_artifact_refs,
                "base": dict(sorted(base_values.items())),
                "candidate": dict(sorted(candidate_values.items())),
                "relative_delta": dict(sorted(relative.items())),
                "expected_direction": direction,
                "profiler_duration_is_reward": False,
            }
        )

    legacy_fields = sorted(
        {field for record in adoption_records for field in record.get("profile", {})}
    )
    adoption_evidence = evaluate_adoption(
        adoption_records,
        legacy_fields=legacy_fields,
        token_counter=token_counter,
    )

    feedback_items = []
    counter, counter_name = (
        (token_counter, "custom") if token_counter is not None else _token_counter()
    )
    feedback_candidates: dict[
        tuple[str, str, str, tuple[str, ...], tuple[tuple[str, int], ...]],
        list[
            tuple[
                tuple[int, int, int, int, int, int, int],
                _Artifact,
                dict[str, Any],
                list[_Artifact],
                str,
                list[tuple[str, int]],
            ]
        ],
    ] = {}
    superseded_final_evals = 0
    excluded_pmu_final_evals = 0
    for final in finals:
        if _capture_family(final.path) == "pmu":
            # The raw payload remains in evidence_catalog, but its profiled
            # elapsed/execution fields can never enter the decision view.
            excluded_pmu_final_evals += 1
            continue
        final_cases = _eval_cases(final.payload)
        inferred_arm = _infer_arm(final.path)
        candidates: list[tuple[int, dict[str, Any], list[_Artifact], str, list[tuple[str, int]]]] = []
        for analysis, group in analyses_internal:
            analysis_op = _operator(analysis["op"])
            arms = {base_arm, *(arm for arm, _ in _analysis_rows(analysis))}
            possible_arms = [inferred_arm] if inferred_arm else sorted(arms)
            for arm in possible_arms:
                if arm not in arms:
                    continue
                case_ids = {
                    int(_case_index(row.get("idx")))
                    for candidate_arm, row in _analysis_rows(analysis)
                    if candidate_arm == arm or arm == base_arm
                }
                selectors = sorted(
                    (analysis_op, case_id)
                    for case_id in case_ids
                    if (analysis_op, case_id) in final_cases
                )
                if not selectors:
                    continue
                related = [
                    artifact
                    for artifact in group
                    if str((artifact.payload.get("metadata") or {}).get("arm")) == arm
                ]
                score = max(
                    (_common_depth(final.path, artifact.path) for artifact in related),
                    default=0,
                )
                candidates.append((score, analysis, group, arm, selectors))
        if not candidates:
            limitations.append(
                {
                    "code": "final_eval_unmatched",
                    "message": f"CANNBench final-eval could not be joined to an Event run: {final.path}",
                    "artifact_refs": [final.ref_id],
                }
            )
            continue
        best_score = max(item[0] for item in candidates)
        best = [item for item in candidates if item[0] == best_score]
        unique: dict[
            tuple[str, str, tuple[str, ...], tuple[tuple[str, int], ...]],
            tuple[Any, ...],
        ] = {}
        for item in best:
            key = (
                str(item[1]["run_id"]),
                item[3],
                _provenance(item[1].get("provenance") or {}),
                tuple(item[4]),
            )
            previous = unique.get(key)
            if previous is None or _analysis_preference(
                item[1], item[3], base_arm
            ) > _analysis_preference(previous[1], previous[3], base_arm):
                unique[key] = item
        if len(unique) != 1:
            rejections.append(
                _rejection(
                    "ambiguous_final_eval",
                    f"CANNBench final-eval matches multiple Event runs: {final.path}",
                    [final.ref_id],
                )
            )
            continue
        _, analysis, event_group, arm, selectors = next(iter(unique.values()))
        logical_key = (
            str(analysis["run_id"]),
            _operator(analysis["op"]),
            arm,
            _provenance(analysis.get("provenance") or {}),
            tuple(selectors),
        )
        family_priority = {"event": 2, None: 1}[_capture_family(final.path)]
        timing_preference = _analysis_preference(analysis, arm, base_arm)
        selection_score = (
            family_priority,
            *timing_preference,
            best_score,
        )
        candidate_source = (
            selection_score,
            final,
            analysis,
            event_group,
            arm,
            selectors,
        )
        feedback_candidates.setdefault(logical_key, []).append(candidate_source)

    feedback_sources = {}
    for logical_key, sources in sorted(feedback_candidates.items()):
        best_score = max(source[0] for source in sources)
        best = [source for source in sources if source[0] == best_score]
        projections: dict[bytes, list[tuple[Any, ...]]] = {}
        for source in best:
            projections.setdefault(
                _selected_final_projection(source[1].payload, source[5]), []
            ).append(source)
        superseded_final_evals += len(sources) - 1
        if len(projections) != 1:
            rejections.append(
                _rejection(
                    "conflicting_final_eval",
                    "equally authoritative final-eval files disagree on selected cases",
                    [source[1].ref_id for source in best],
                )
            )
            continue
        feedback_sources[logical_key] = min(
            best, key=lambda source: str(source[1].path).casefold()
        )

    feedback_coverage: set[tuple[str, str, str, tuple[str, ...], int]] = set()
    for _, final, analysis, event_group, arm, selectors in (
        feedback_sources[key] for key in sorted(feedback_sources)
    ):
        matching_pmus = [
            artifact
            for artifact in pmus
            if str(analysis["run_id"]) not in conflicting_source_run_ids
            and str(artifact.payload["metadata"]["run_id"]) == str(analysis["run_id"])
            and _operator(artifact.payload["metadata"]["operator"]) == _operator(analysis["op"])
            and str(artifact.payload["metadata"]["arm"]) == arm
            and str(artifact.payload["metadata"]["device"])
            == str((analysis.get("capture_metadata") or {}).get("device"))
            and _provenance(artifact.payload["metadata"])
            == _provenance(analysis.get("provenance") or {})
            and _case_index(artifact.payload["metadata"]["case_id"])
            in {case_id for _, case_id in selectors}
        ]
        try:
            compact = compact_feedback(
                final.payload,
                [artifact.payload for artifact in matching_pmus],
                selected_cases=selectors,
            )
        except ValueError as exc:
            rejections.append(
                _rejection("compact_feedback_conflict", str(exc), [final.ref_id])
            )
            continue
        compact["token_audit"] = {
            "counter": counter_name,
            **token_audit(
                final.payload,
                [artifact.payload for artifact in matching_pmus],
                compact,
                counter,
            ),
        }
        feedback_items.append(
            {
                "run_id": analysis["run_id"],
                "arm": arm,
                "artifact_refs": sorted(
                    {
                        final.ref_id,
                        *(artifact.ref_id for artifact in event_group),
                        *(artifact.ref_id for artifact in matching_pmus),
                    }
                ),
                "feedback": compact,
            }
        )
        for _, case_id in selectors:
            feedback_coverage.add(
                (
                    str(analysis["run_id"]),
                    _operator(analysis["op"]),
                    arm,
                    _provenance(analysis.get("provenance") or {}),
                    case_id,
                )
            )

    expected_feedback: set[tuple[str, str, str, tuple[str, ...], int]] = set()
    for analysis, _ in analyses_internal:
        identity = (
            str(analysis["run_id"]),
            _operator(analysis["op"]),
            _provenance(analysis.get("provenance") or {}),
        )
        for arm, row in _analysis_rows(analysis):
            case_id = int(_case_index(row.get("idx")))
            for expected_arm in (base_arm, str(arm)):
                expected_feedback.add(
                    (identity[0], identity[1], expected_arm, identity[2], case_id)
                )
    missing_feedback = sorted(expected_feedback - feedback_coverage)
    if missing_feedback:
        limitations.append(
            {
                "code": "final_eval_missing",
                "message": "one or more Event timing arms/cases lack an authoritative final-eval",
                "missing": [
                    {
                        "run_id": run_id,
                        "operator": operator,
                        "arm": arm,
                        "provenance": dict(zip(ADOPTION_PROVENANCE_FIELDS, provenance)),
                        "case_id": case_id,
                    }
                    for run_id, operator, arm, provenance, case_id in missing_feedback
                ],
            }
        )

    correctness_summary = _correctness_summary(feedback_items)
    correctness_summary["complete"] = not missing_feedback
    if missing_feedback:
        correctness_summary["missing"] = limitations[-1]["missing"]

    if not events:
        limitations.append({"code": "event_timing_missing", "message": "no valid Event timing artifacts"})
    if not pmus:
        limitations.append({"code": "pmu_missing", "message": "no valid directed PMU artifacts"})
    if not finals:
        limitations.append({"code": "cannbench_final_missing", "message": "no valid CANNBench final-eval artifacts"})
    elif excluded_pmu_final_evals == len(finals):
        limitations.append(
            {
                "code": "profiler_off_final_missing",
                "message": "only profiler-perturbed final-eval artifacts were found",
            }
        )
    if not analyses_internal:
        limitations.append({"code": "timing_analysis_missing", "message": "no valid profiler-off timing A/B analysis"})
    if not adoption_records:
        unstable = any(
            row.get("stable") is False
            for analysis, _ in analyses_internal
            for _, row in _analysis_rows(analysis)
        )
        notices.append(
            {
                "code": "no_labeled_adoption_records",
                "message": (
                    "unstable timing pairs were deliberately not labeled"
                    if unstable
                    else "no timing/PMU pair was eligible for adoption evidence"
                ),
            }
        )
    if not run_metadata:
        notices.append(
            {
                "code": "run_metadata_missing",
                "message": (
                    "legacy artifacts have no run_metadata.json; source-tree hashes "
                    "cannot be reconstructed"
                ),
            }
        )

    useful = bool(analyses_internal or pmu_comparisons or feedback_items)
    complete_kinds = bool(
        events
        and pmus
        and finals
        and analyses_internal
        and pmu_comparisons
        and feedback_items
    )
    if not useful:
        status = "rejected"
    elif rejections or limitations or not complete_kinds:
        status = "partial"
    else:
        status = "ok"

    raw_tokens = sum(
        int((item["feedback"].get("token_audit") or {}).get("raw_tokens") or 0)
        for item in feedback_items
    )
    compact_tokens = sum(
        int((item["feedback"].get("token_audit") or {}).get("compact_tokens") or 0)
        for item in feedback_items
    )
    evidence_catalog = _evidence_catalog(discovered_artifacts, rejections)
    evidence_catalog_tokens = counter(
        json.dumps(evidence_catalog, ensure_ascii=False, sort_keys=True)
    )

    result = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "query": {
            "action_cell": action_cell,
            "base_arm": base_arm,
            "timing_stat": stat,
            "mode": "single local query after profiler completion",
        },
        "artifacts": {
            "roots": [str(root) for root in roots],
            "counts": {
                "event": len(events),
                "pmu": len(pmus),
                "cannbench_final": len(finals),
                "run_metadata": len(run_metadata),
            },
            "duplicates_ignored": duplicates_ignored,
            "superseded_final_evals": superseded_final_evals,
            "profiler_perturbed_final_evals_excluded": excluded_pmu_final_evals,
            "superseded_run_metadata": superseded_run_metadata,
            "refs": artifact_refs,
        },
        "run_contexts": run_contexts,
        "evidence_catalog": evidence_catalog,
        "correctness": correctness_summary,
        "timing": {
            "authority": "profiler-off NPU Event",
            "analyses": [analysis for analysis, _ in analyses_internal],
        },
        "pmu": {
            "role": "mechanism attribution only; profiler Duration is excluded as reward",
            "comparisons": pmu_comparisons,
        },
        "compact_feedback": feedback_items,
        "token_efficiency": {
            "scope": "serialized compact feedback inputs/outputs only",
            "counter": counter_name,
            "raw_tokens": raw_tokens,
            "compact_tokens": compact_tokens,
            "reduction": (
                1.0 - compact_tokens / raw_tokens if raw_tokens else None
            ),
            "optimizer_output_tokens_included": False,
            "full_evidence_embedded": True,
            "unified_output_reduction_claimed": False,
            "evidence_catalog_tokens": evidence_catalog_tokens,
            "unified_output_tokens": None,
            "tradeoff": (
                "compact decision view is measured separately; the default unified "
                "output embeds all parsed evidence and is intentionally lossless, not "
                "claimed to be smaller than its sources"
            ),
        },
        "adoption_evidence": adoption_evidence,
        "rejections": rejections,
        "limitations": limitations,
        "notices": notices,
    }
    # The count includes the complete self-contained output.  Iterate because
    # the decimal token count is itself part of the serialized object.
    for _ in range(10):
        measured = counter(serialize_feedback(result))
        if result["token_efficiency"]["unified_output_tokens"] == measured:
            break
        result["token_efficiency"]["unified_output_tokens"] = measured
    return result


# A concise alias for callers that treat this module as an SDK rather than CLI.
query_completed_run = query_profiler_feedback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", action="append", required=True, type=Path)
    parser.add_argument("--action-cell", required=True)
    parser.add_argument("--base-arm", default="base")
    parser.add_argument("--stat", default="p50", choices=("p50", "p95", "p99"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    payload = query_profiler_feedback(
        args.artifact_root,
        action_cell=args.action_cell,
        base_arm=args.base_arm,
        stat=args.stat,
    )
    if args.output.exists():
        print(f"refusing existing output: {args.output}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialize_feedback(payload), encoding="utf-8")
    print(f"wrote {args.output}")
    return {"ok": 0, "partial": 3, "rejected": 4}[payload["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
