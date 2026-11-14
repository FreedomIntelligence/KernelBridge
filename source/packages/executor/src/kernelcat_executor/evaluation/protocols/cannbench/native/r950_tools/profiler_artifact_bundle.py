#!/usr/bin/env python3
"""Export completed NPU-profiler artifacts as an auditable offline bundle.

The exporter deliberately has no remote-client imports and never runs an NPU
job.  It copies the textual evidence produced by completed runs byte for byte
and adds a per-root ``kernel_eval.json`` / self-contained ``kernel_eval.html``
view.  The derived views are conveniences only: every JSON, CSV, and log that
they reference remains in ``raw/`` with a SHA-256 in ``manifest.json``.

Example::

    python profiler_artifact_bundle.py --artifact-root /path/to/completed-capture \
        --unified-feedback /path/to/unified_feedback.json --output-dir /path/to/export

``--output-dir`` must not exist.  This prevents an export from silently
overwriting or mixing evidence from two runs.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "profiler-artifact-bundle/v1"
KERNEL_EVAL_SCHEMA_VERSION = "profiler-artifact-kernel-eval/v1"
TEXT_SUFFIXES = {".json", ".csv", ".log"}
ARCHIVE_SUFFIXES = (".tgz", ".tar.gz", ".tar", ".zip")
EVALUATION_ROLES = ("correctness", "performance", "final")
CAPTURE_FAMILIES = ("event", "pmu", "unclassified")


@dataclass(frozen=True)
class RawFile:
    """One byte-for-byte copied evidence file."""

    root_id: str
    source: Path
    source_relative_path: str
    bundle_path: str
    role: str
    sha256: str
    size_bytes: int


def _json_dump(value: Any) -> str:
    """Canonical, deterministic on-disk JSON representation."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dump(value), encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return (slug or "artifact")[:72]


def _safe_file_name(name: str) -> str:
    """Keep a recognizable extension while bounding Windows path length."""
    suffix = "".join(Path(name).suffixes).casefold()
    stem = name[: -len(suffix)] if suffix else name
    return _safe_slug(stem)[:64] + suffix


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _role_for(relative: Path) -> str:
    name = relative.name.casefold()
    if name.startswith("unified_feedback") and name.endswith(".json"):
        return "raw_unified_feedback"
    if any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES):
        return "raw_profiler_archive"
    if name == "event_samples.json":
        return "raw_event_samples"
    if name == "pipeprof.json":
        return "raw_pmu_summary"
    if name.startswith("cann_correctness_eval") and name.endswith(".json"):
        return "raw_cannbench_correctness_eval"
    if name.startswith("cann_performance_eval") and name.endswith(".json"):
        return "raw_cannbench_performance_eval"
    if name.startswith("cann_final_eval") and name.endswith(".json"):
        return "raw_cannbench_final_eval"
    if name == "profiler_info.json":
        return "raw_profiler_info"
    if name == "profiler_metadata.json":
        return "raw_profiler_metadata"
    if name == "kernel_details.csv":
        return "raw_kernel_details_csv"
    if relative.suffix.casefold() == ".log":
        return "raw_evaluation_log"
    return "raw_text_evidence"


def _capture_family(relative_path: str) -> str:
    components = [part.casefold() for part in Path(relative_path).parts]
    if any("pmu" in part or "pipeprof" in part for part in components):
        return "pmu"
    if any("event" in part for part in components):
        return "event"
    return "unclassified"


def _normalise_arm(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if text in {"base", "baseline", "control", "naive"}:
        return "base"
    if text in {"mt", "candidate", "optimized", "optimised", "tuned"}:
        return "mt"
    return None


def _arm_for(relative_path: str, payload: Mapping[str, Any] | None = None) -> str:
    if isinstance(payload, Mapping):
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping):
            arm = _normalise_arm(metadata.get("arm"))
            if arm:
                return arm
        arm = _normalise_arm(payload.get("arm"))
        if arm:
            return arm
    # The completed-run layout is intentionally simple (event3_base,
    # pmu2_mt, ...).  Match whole underscore/dash-delimited tokens only.
    for part in reversed(Path(relative_path).parts[:-1]):
        tokens = [token for token in re.split(r"[^a-z0-9]+", part.casefold()) if token]
        if "base" in tokens or "baseline" in tokens:
            return "base"
        if any(token in {"mt", "candidate", "optimized", "optimised", "tuned"} for token in tokens):
            return "mt"
    return "unknown"


def _evaluation_role(relative_path: str) -> str | None:
    name = Path(relative_path).name.casefold()
    if name.startswith("cann_correctness_eval") and name.endswith(".json"):
        return "correctness"
    if name.startswith("cann_performance_eval") and name.endswith(".json"):
        return "performance"
    if name.startswith("cann_final_eval") and name.endswith(".json"):
        return "final"
    return None


def _discover_root(
    root: Path, root_id: str, *, include_archives: bool
) -> tuple[list[RawFile], list[dict[str, str]]]:
    """Find safe text evidence without following symlinks outside ``root``."""
    raw_files: list[RawFile] = []
    omitted: list[dict[str, str]] = []
    for source in sorted(root.rglob("*"), key=lambda item: _portable(item.relative_to(root)).casefold()):
        if source.is_symlink():
            omitted.append(
                {
                    "source_root_id": root_id,
                    "source_relative_path": _portable(source.relative_to(root)),
                    "reason": "symlink is not followed",
                }
            )
            continue
        if not source.is_file():
            continue
        resolved = source.resolve()
        if not _inside(resolved, root):
            omitted.append(
                {
                    "source_root_id": root_id,
                    "source_relative_path": _portable(source.relative_to(root)),
                    "reason": "resolved path escapes artifact root",
                }
            )
            continue
        relative = source.relative_to(root)
        suffix = source.suffix.casefold()
        is_archive = any(source.name.casefold().endswith(item) for item in ARCHIVE_SUFFIXES)
        if suffix not in TEXT_SUFFIXES and not (include_archives and is_archive):
            if is_archive:
                omitted.append(
                    {
                        "source_root_id": root_id,
                        "source_relative_path": _portable(relative),
                        "reason": "archive intentionally not copied; extracted textual evidence is copied",
                    }
                )
            continue
        relative_text = _portable(relative)
        # Artifact trees contain CANN's deeply nested profiler output.  Keeping
        # its original directories under a worktree staging directory exceeds
        # the 260-character Windows path limit.  Use a stable, shallow raw
        # layout and retain the exact source-relative path in the manifest.
        bundle_path = f"runs/{root_id}/raw/{len(raw_files) + 1:03d}_{_safe_file_name(source.name)}"
        raw_files.append(
            RawFile(
                root_id=root_id,
                source=source,
                source_relative_path=relative_text,
                bundle_path=bundle_path,
                role=_role_for(relative),
                sha256=_sha256(source),
                size_bytes=source.stat().st_size,
            )
        )
    return raw_files, omitted


def _copy_raw_file(raw: RawFile, staging: Path) -> None:
    destination = staging / Path(raw.bundle_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # ``bundle_path`` is constructed from a validated relative source path;
    # retain this check as a guard against future changes to the layout.
    if not _inside(destination.resolve(), staging.resolve()):
        raise ValueError(f"refusing to write outside bundle: {raw.bundle_path}")
    shutil.copyfile(raw.source, destination)
    copied_sha256 = _sha256(destination)
    if copied_sha256 != raw.sha256:
        raise RuntimeError(f"copy digest mismatch for {raw.source}")


def _read_json(raw: RawFile) -> tuple[Any | None, str | None]:
    try:
        return json.loads(raw.source.read_text(encoding="utf-8-sig")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quantile(values: list[float], quantile: float) -> float:
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def _event_statistics(samples: Any) -> dict[str, Any]:
    if not isinstance(samples, list):
        return {"available": False, "reason": "samples_us is not an array"}
    values = [_finite_number(value) for value in samples]
    if any(value is None for value in values):
        return {
            "available": False,
            "reason": "samples_us contains a non-finite value",
            "sample_count": len(samples),
        }
    numbers = sorted(value for value in values if value is not None)
    if not numbers:
        return {"available": False, "reason": "samples_us is empty", "sample_count": 0}
    p50 = _quantile(numbers, 0.5)
    mad = _quantile(sorted(abs(value - p50) for value in numbers), 0.5)
    p95 = _quantile(numbers, 0.95)
    return {
        "available": True,
        "sample_count": len(numbers),
        "min_us": numbers[0],
        "max_us": numbers[-1],
        "mean_us": sum(numbers) / len(numbers),
        "p50_us": p50,
        "p95_us": p95,
        "p99_us": _quantile(numbers, 0.99),
        "mad_us": mad,
        "mad_over_p50": mad / p50 if p50 else None,
        "p95_over_p50": p95 / p50 if p50 else None,
    }


def _case_summary(case: Mapping[str, Any]) -> dict[str, Any]:
    accuracy = case.get("accuracy")
    if not isinstance(accuracy, Mapping):
        accuracy = {}
    accuracy_metadata = accuracy.get("metadata")
    if not isinstance(accuracy_metadata, Mapping):
        accuracy_metadata = {}
    device_kernels = None
    op_times = case.get("op_times")
    if isinstance(op_times, Mapping) and isinstance(op_times.get("device_kernels"), Mapping):
        device_kernels = op_times["device_kernels"]
    return {
        key: case.get(key)
        for key in (
            "case_id",
            "status",
            "elapsed_us",
            "baseline_perf_us",
            "t_hw_us",
            "speedup",
            "perf_score",
            "failure_type",
            "error_msg",
            "performance_error_msg",
        )
    } | {
        "correctness": {
            "passed": accuracy.get("passed"),
            "threshold": accuracy.get("threshold"),
            "error_msg": accuracy.get("error_msg"),
            "metadata": dict(accuracy_metadata),
        },
        "device_kernels": dict(device_kernels) if device_kernels is not None else None,
    }


def _evaluation_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    top_level = {
        key: payload.get(key)
        for key in (
            "framework_version",
            "tasks_version",
            "eval_code",
            "timestamp",
            "device",
            "total_operators",
            "total_cases",
            "passed_cases",
            "failed_cases",
            "overall_score",
            "result_stage",
            "contains_performance",
        )
        if key in payload
    }
    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        top_level["summary"] = dict(summary)
    operators = []
    for operator in payload.get("operators") or []:
        if not isinstance(operator, Mapping):
            continue
        operators.append(
            {
                "operator": operator.get("operator"),
                "rel_path": operator.get("rel_path"),
                "total_cases": operator.get("total_cases"),
                "passed_cases": operator.get("passed_cases"),
                "failed_cases": operator.get("failed_cases"),
                "pass_rate": operator.get("pass_rate"),
                "avg_speedup": operator.get("avg_speedup"),
                "score": operator.get("score"),
                "performance_score": operator.get("performance_score"),
                "cases": [
                    _case_summary(case)
                    for case in operator.get("cases") or []
                    if isinstance(case, Mapping)
                ],
            }
        )
    return {"top_level": top_level, "operators": operators}


def _pmu_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    # Keep every PMU field in raw_json_documents.  This concise projection is
    # what the HTML renders first, so an agent sees the usable counters before
    # opening a potentially large CSV.
    fields = (
        "schema_version",
        "capture_kind",
        "metric_group",
        "available",
        "unavailable_reason",
        "timing_authority",
        "kernel_count",
        "all_kernel_count",
        "launch_count",
        "excluded_launch_count",
        "selected_columns",
        "aggregate_metrics",
        "duration_weighted_metrics",
        "context_metrics",
        "derived_metrics",
        "excluded_kernels",
        "metadata",
        "source_files",
    )
    return {key: payload.get(key) for key in fields if key in payload}


def _kernel_eval(root_id: str, source_root: Path, raw_files: list[RawFile]) -> dict[str, Any]:
    arms: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "event_samples": [],
            "pmu": [],
            "evaluations": {
                family: {role: [] for role in EVALUATION_ROLES}
                for family in CAPTURE_FAMILIES
            },
        }
    )
    raw_json_documents = []
    parse_errors = []

    for raw in raw_files:
        if raw.source.suffix.casefold() != ".json":
            continue
        payload, parse_error = _read_json(raw)
        document = {
            "raw_path": _portable(Path(raw.bundle_path).relative_to(f"runs/{root_id}")),
            "sha256": raw.sha256,
            "role": raw.role,
        }
        if parse_error:
            document["parse_error"] = parse_error
            parse_errors.append({"raw_path": document["raw_path"], "error": parse_error})
        else:
            # Keeping the parsed JSON here is intentional.  The derived view
            # is not token-minimised; it is a one-file, complete JSON view in
            # addition to the byte-for-byte raw files.
            document["content"] = payload
        raw_json_documents.append(document)
        if not isinstance(payload, Mapping):
            continue

        raw_path = document["raw_path"]
        relative_source = raw.source_relative_path
        capture = _capture_family(relative_source)
        arm = _arm_for(relative_source, payload)
        if raw.role == "raw_event_samples":
            metadata = payload.get("metadata")
            arms[arm]["event_samples"].append(
                {
                    "raw_path": raw_path,
                    "sha256": raw.sha256,
                    "capture": capture,
                    "metadata": dict(metadata) if isinstance(metadata, Mapping) else metadata,
                    "profiler_enabled": payload.get("profiler_enabled"),
                    "timer": payload.get("timer"),
                    "unit": payload.get("unit"),
                    "samples_us": payload.get("samples_us"),
                    "statistics": _event_statistics(payload.get("samples_us")),
                }
            )
        elif raw.role == "raw_pmu_summary":
            arms[arm]["pmu"].append(
                {
                    "raw_path": raw_path,
                    "sha256": raw.sha256,
                    "capture": capture,
                    "summary": _pmu_summary(payload),
                }
            )

        evaluation_role = _evaluation_role(relative_source)
        if evaluation_role:
            arms[arm]["evaluations"][capture][evaluation_role].append(
                {
                    "raw_path": raw_path,
                    "sha256": raw.sha256,
                    "summary": _evaluation_summary(payload),
                }
            )

    inventory = [
        {
            "raw_path": _portable(Path(raw.bundle_path).relative_to(f"runs/{root_id}")),
            "source_relative_path": raw.source_relative_path,
            "sha256": raw.sha256,
            "size_bytes": raw.size_bytes,
            "role": raw.role,
        }
        for raw in raw_files
    ]
    return {
        "schema_version": KERNEL_EVAL_SCHEMA_VERSION,
        "derived": True,
        "derived_from": {
            "description": (
                "Derived from the byte-for-byte raw JSON/CSV/log evidence in raw/. "
                "This JSON is intentionally complete for raw JSON documents; raw files remain authoritative."
            ),
            "source_root_label": source_root.name,
            "source_root_id": root_id,
            "raw_file_count": len(raw_files),
            "raw_json_document_count": len(raw_json_documents),
        },
        "raw_inventory": inventory,
        "raw_json_documents": raw_json_documents,
        "parse_errors": parse_errors,
        "arms": dict(sorted(arms.items())),
    }


def _link(raw_path: str) -> str:
    return f'<a href="{html.escape(raw_path, quote=True)}">{html.escape(raw_path)}</a>'


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6g}"
    return html.escape(str(value))


def _json_pretty(value: Any) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _html_table(headers: list[str], rows: Iterable[Iterable[str]]) -> str:
    rendered_rows = list(rows)
    if not rendered_rows:
        return "<p class=empty>None recorded.</p>"
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rendered_rows
    )
    return f"<div class=table-wrap><table><thead><tr>{header_html}</tr></thead><tbody>{body}</tbody></table></div>"


def _evaluation_rows(arms: Mapping[str, Any]) -> list[list[str]]:
    rows = []
    for arm, arm_data in sorted(arms.items()):
        evaluations = arm_data.get("evaluations") if isinstance(arm_data, Mapping) else {}
        for capture in CAPTURE_FAMILIES:
            by_role = evaluations.get(capture, {}) if isinstance(evaluations, Mapping) else {}
            for role in EVALUATION_ROLES:
                for entry in by_role.get(role, []) if isinstance(by_role, Mapping) else []:
                    summary = entry.get("summary", {})
                    header = summary.get("top_level", {}) if isinstance(summary, Mapping) else {}
                    case_count = header.get("total_cases", "—")
                    passed = header.get("passed_cases", "—")
                    failed = header.get("failed_cases", "—")
                    rows.append(
                        [
                            html.escape(arm),
                            html.escape(capture),
                            html.escape(role),
                            _link(entry["raw_path"]),
                            _text(case_count),
                            _text(passed),
                            _text(failed),
                            _text(header.get("overall_score")),
                        ]
                    )
    return rows


def _event_rows(arms: Mapping[str, Any]) -> list[list[str]]:
    rows = []
    for arm, arm_data in sorted(arms.items()):
        for entry in arm_data.get("event_samples", []) if isinstance(arm_data, Mapping) else []:
            metadata = entry.get("metadata") or {}
            stats = entry.get("statistics") or {}
            rows.append(
                [
                    html.escape(arm),
                    html.escape(str(entry.get("capture") or "—")),
                    _link(entry["raw_path"]),
                    _text(metadata.get("run_id")),
                    _text(metadata.get("operator")),
                    _text(metadata.get("case_id")),
                    _text(metadata.get("warmup")),
                    _text(metadata.get("inner_repeat", 1)),
                    _text(metadata.get("timing_scope", "single_call")),
                    _text(metadata.get("cache_clean_each_sample")),
                    _text(stats.get("sample_count")),
                    _text(stats.get("p50_us")),
                    _text(stats.get("p95_us")),
                    _text(stats.get("p99_us")),
                    _text(stats.get("mad_us")),
                ]
            )
    return rows


def _pmu_rows(arms: Mapping[str, Any]) -> tuple[list[list[str]], list[list[str]]]:
    captures = []
    metrics = []
    for arm, arm_data in sorted(arms.items()):
        for entry in arm_data.get("pmu", []) if isinstance(arm_data, Mapping) else []:
            summary = entry.get("summary") or {}
            metadata = summary.get("metadata") or {}
            captures.append(
                [
                    html.escape(arm),
                    _link(entry["raw_path"]),
                    _text(summary.get("available")),
                    _text(summary.get("metric_group")),
                    _text(summary.get("kernel_count")),
                    _text(summary.get("launch_count")),
                    _text(summary.get("timing_authority")),
                    _text(metadata.get("patch_verified")),
                ]
            )
            for section in ("duration_weighted_metrics", "aggregate_metrics", "context_metrics", "derived_metrics"):
                values = summary.get(section)
                if not isinstance(values, Mapping):
                    continue
                for metric, statistic in sorted(values.items()):
                    if isinstance(statistic, Mapping):
                        representative = statistic.get("duration_weighted_mean")
                        if representative is None:
                            representative = statistic.get("p50", statistic.get("mean"))
                    else:
                        representative = statistic
                    metrics.append(
                        [
                            html.escape(arm),
                            html.escape(section),
                            html.escape(str(metric)),
                            _text(representative),
                            _link(entry["raw_path"]),
                        ]
                    )
    return captures, metrics


def _render_html(kernel_eval: Mapping[str, Any]) -> str:
    """Render a dependency-free report; raw evidence is linked locally only."""
    derived_from = kernel_eval.get("derived_from") or {}
    arms = kernel_eval.get("arms") or {}
    eval_rows = _evaluation_rows(arms)
    event_rows = _event_rows(arms)
    pmu_captures, pmu_metrics = _pmu_rows(arms)
    inventory_rows = [
        [
            _link(item["raw_path"]),
            html.escape(str(item.get("role") or "—")),
            _text(item.get("size_bytes")),
            f"<code>{html.escape(str(item.get('sha256') or ''))}</code>",
        ]
        for item in kernel_eval.get("raw_inventory") or []
    ]
    parse_errors = kernel_eval.get("parse_errors") or []
    error_html = ""
    if parse_errors:
        error_html = (
            "<h2>JSON parse notices</h2><pre>"
            + _json_pretty(parse_errors)
            + "</pre>"
        )
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kernel evaluation evidence</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 2rem auto; max-width: 1500px; padding: 0 1rem; line-height: 1.45; }
h1, h2 { margin-top: 1.7rem; }
.notice { border-left: 4px solid #357edd; padding: .85rem 1rem; background: color-mix(in srgb, #357edd 10%, transparent); }
.table-wrap { overflow-x: auto; border: 1px solid #8885; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th, td { padding: .45rem .55rem; text-align: left; vertical-align: top; border-bottom: 1px solid #8884; white-space: nowrap; }
th { position: sticky; top: 0; background: Canvas; }
code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
pre { overflow: auto; padding: 1rem; background: #8882; }
.empty { color: #777; font-style: italic; }
</style>
</head>
<body>
<h1>Kernel evaluation evidence</h1>
<div class="notice"><strong>Derived from raw JSON.</strong> This static page is derived from the byte-for-byte JSON, CSV, and log files in <code>raw/</code>. It does not upload, query, or fetch anything. Raw files and SHA-256 digests in <code>../../manifest.json</code> are authoritative.</div>
<p>Source label: <code>""" + html.escape(str(derived_from.get("source_root_label") or "—")) + """</code>; raw files: <strong>""" + _text(derived_from.get("raw_file_count")) + """</strong>; JSON documents embedded in <code>kernel_eval.json</code>: <strong>""" + _text(derived_from.get("raw_json_document_count")) + """</strong>.</p>
<h2>Base / MT CANNBench evaluations</h2>
""" + _html_table(
        ["arm", "capture", "evaluation", "raw JSON", "cases", "passed", "failed", "score"],
        eval_rows,
    ) + """
<p>Event-family final evaluation supplies the profiler-off timing context. PMU-family evaluation files are shown as audit evidence only; PMU duration is not a latency reward.</p>
<h2>Profiler-off NPU Event sampling</h2>
""" + _html_table(
        ["arm", "capture", "raw JSON", "run", "operator", "case", "warmup", "inner repeat", "scope", "cache clean", "n", "p50 µs", "p95 µs", "p99 µs", "MAD µs"],
        event_rows,
    ) + """
<h2>Directed PMU captures</h2>
""" + _html_table(
        ["arm", "raw JSON", "available", "metric group", "kernels", "launches", "timing authority", "patch verified"],
        pmu_captures,
    ) + """
<h3>PMU metric summaries</h3>
""" + _html_table(
        ["arm", "section", "metric", "representative value", "raw JSON"],
        pmu_metrics,
    ) + """<h2>Byte-for-byte raw evidence inventory</h2>
""" + _html_table(["raw path", "role", "bytes", "SHA-256"], inventory_rows) + error_html + """
<h2>Derived-view contract</h2>
<pre>""" + _json_pretty(
        {
            "schema_version": kernel_eval.get("schema_version"),
            "derived": kernel_eval.get("derived"),
            "derived_from": derived_from,
            "raw_json_note": "Full parsed raw JSON is in kernel_eval.json.raw_json_documents; byte-for-byte sources are in raw/.",
        }
    ) + """</pre>
</body>
</html>
"""


def _manifest_entry(raw: RawFile) -> dict[str, Any]:
    return {
        "path": raw.bundle_path,
        "source_root_id": raw.root_id,
        "source_relative_path": raw.source_relative_path,
        "sha256": raw.sha256,
        "size_bytes": raw.size_bytes,
        "role": raw.role,
        "derived": False,
    }


def _derived_manifest_entry(path: Path, staging: Path, root_id: str, role: str) -> dict[str, Any]:
    return {
        "path": _portable(path.relative_to(staging)),
        "source_root_id": root_id,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "role": role,
        "derived": True,
    }


def _supplemental_unified_feedback(
    files: Iterable[str | Path],
) -> list[RawFile]:
    """Add a one-shot SDK result without pretending it is a fourth NPU run."""
    result: list[RawFile] = []
    seen: set[str] = set()
    for index, raw_path in enumerate(files, start=1):
        source = Path(raw_path).expanduser().resolve()
        identity = str(source).casefold()
        if identity in seen:
            raise ValueError(f"duplicate unified feedback file: {source}")
        seen.add(identity)
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(
                f"unified feedback must be a regular local file: {source}"
            )
        if source.suffix.casefold() != ".json":
            raise ValueError(f"unified feedback must be JSON: {source}")
        relative_name = _safe_slug(source.stem) + source.suffix.casefold()
        bundle_path = f"supplemental/unified_feedback_{index:02d}_{relative_name}"
        result.append(
            RawFile(
                root_id="supplemental",
                source=source,
                source_relative_path=source.name,
                bundle_path=bundle_path,
                role="raw_unified_feedback",
                sha256=_sha256(source),
                size_bytes=source.stat().st_size,
            )
        )
    return result


def export_artifact_bundle(
    artifact_roots: Iterable[str | Path],
    output_dir: str | Path,
    *,
    include_archives: bool = False,
    unified_feedback_files: Iterable[str | Path] = (),
) -> dict[str, Any]:
    """Build an offline, non-overwriting profiler evidence bundle.

    The return value is exactly the JSON written to ``manifest.json``.  The
    function only reads artifact roots and writes a new output directory; it
    has no network or remote-execution side effects.
    """
    roots = [Path(root).expanduser().resolve() for root in artifact_roots]
    if not roots:
        raise ValueError("at least one artifact root is required")
    if len({str(root).casefold() for root in roots}) != len(roots):
        raise ValueError("artifact roots must be unique")
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"artifact root is not a directory: {root}")

    output = Path(output_dir).expanduser()
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    output = output.resolve()
    for root in roots:
        if _inside(output, root):
            raise ValueError("output directory must not be inside an artifact root")
    output.parent.mkdir(parents=True, exist_ok=True)

    root_records = []
    all_raw_files: list[RawFile] = []
    omitted: list[dict[str, str]] = []
    seen_root_ids: set[str] = set()
    for index, root in enumerate(roots, start=1):
        stem = _safe_slug(root.name)
        root_id = f"run_{index:02d}_{stem}"
        while root_id.casefold() in seen_root_ids:
            root_id = f"{root_id}_x"
        seen_root_ids.add(root_id.casefold())
        raw_files, root_omitted = _discover_root(
            root, root_id, include_archives=include_archives
        )
        root_records.append(
            {"id": root_id, "source_root_label": root.name, "raw_file_count": len(raw_files)}
        )
        all_raw_files.extend(raw_files)
        omitted.extend(root_omitted)
    supplemental_files = _supplemental_unified_feedback(unified_feedback_files)
    all_raw_files.extend(supplemental_files)

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        for raw in all_raw_files:
            _copy_raw_file(raw, staging)

        entries = [_manifest_entry(raw) for raw in all_raw_files]
        for record, root in zip(root_records, roots):
            root_id = record["id"]
            root_raw_files = [raw for raw in all_raw_files if raw.root_id == root_id]
            kernel_eval = _kernel_eval(root_id, root, root_raw_files)
            run_dir = staging / "runs" / root_id
            json_path = run_dir / "kernel_eval.json"
            html_path = run_dir / "kernel_eval.html"
            _write_json(json_path, kernel_eval)
            html_path.write_text(_render_html(kernel_eval), encoding="utf-8", newline="\n")
            entries.append(_derived_manifest_entry(json_path, staging, root_id, "kernel_eval_json"))
            entries.append(_derived_manifest_entry(html_path, staging, root_id, "kernel_eval_html"))

        entries.sort(key=lambda entry: entry["path"].casefold())
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "derived": True,
            "description": (
                "Offline artifact bundle. Raw JSON/CSV/log entries are byte-for-byte copies; "
                "kernel_eval JSON/HTML entries are derived views and never replace raw evidence. "
                "Raw paths use a shallow numbered layout for Windows path-length safety; "
                "source_relative_path records the original artifact-tree path."
            ),
            "network_access": False,
            "archives_included": include_archives,
            "supplemental_unified_feedback": [
                {
                    "path": raw.bundle_path,
                    "sha256": raw.sha256,
                    "size_bytes": raw.size_bytes,
                }
                for raw in supplemental_files
            ],
            "input_roots": root_records,
            "entries": entries,
            "omitted": sorted(omitted, key=lambda item: (item["source_root_id"], item["source_relative_path"])),
        }
        _write_json(staging / "manifest.json", manifest)
        # Atomic publish keeps callers from observing a partial export.  The
        # target was checked above and is checked again in case another process
        # raced us while the staging directory was being built.
        if output.exists():
            raise FileExistsError(f"output directory appeared during export: {output}")
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-root",
        action="append",
        required=True,
        help="completed local artifact root; may be supplied more than once",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="new output directory (must not already exist)",
    )
    parser.add_argument(
        "--include-archives",
        action="store_true",
        help="also copy .tgz/.tar.gz/.tar/.zip verbatim; archives are never extracted",
    )
    parser.add_argument(
        "--unified-feedback",
        action="append",
        default=[],
        help="optional local unified_feedback.json from the one-shot SDK; may be repeated",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        manifest = export_artifact_bundle(
            args.artifact_root,
            args.output_dir,
            include_archives=args.include_archives,
            unified_feedback_files=args.unified_feedback,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"profiler artifact export failed: {exc}", file=sys.stderr)
        return 2
    print(
        _json_dump(
            {
                "status": "ok",
                "output_dir": str(Path(args.output_dir).expanduser()),
                "manifest": "manifest.json",
                "raw_files": sum(1 for entry in manifest["entries"] if not entry["derived"]),
                "derived_files": sum(1 for entry in manifest["entries"] if entry["derived"]),
            }
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
