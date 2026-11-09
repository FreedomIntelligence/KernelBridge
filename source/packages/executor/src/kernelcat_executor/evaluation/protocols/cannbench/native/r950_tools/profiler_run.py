#!/usr/bin/env python3
"""Closed-loop 910C Event + multi-group directed-PMU profiler runner.

The frozen Stage-1 execution engine remains in :mod:`profiler_run_core`.  This
compatibility layer collects a diagnostic metric pack, produces source/route
identity, and publishes a diagnosis-first Agent entrypoint.  Profiler-off Event
remains the only latency authority; PMU is mechanism evidence only.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import profiler_run_core as _core
from profiler_run_publish import enrich_completed_run, publish_enrichment_failure
from profiler_run_support import (
    DEFAULT_DIAGNOSTIC_METRICS,
    METRIC_PLAN_SCHEMA,
    SUPPORTED_METRIC_GROUPS,
    dedupe_metrics as _dedupe_metrics,
    metric_slug as _metric_slug,
    multi_pmu_command as _multi_pmu_command,
    option_value as _option_value,
    patched_core as _patched_core_impl,
    replace_option as _replace_option,
    strip_metrics as _strip_metrics,
)

for _name in dir(_core):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_core, _name))

RUNNER_NAME = "r950_tools/profiler_run.py"


def _main_impl(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cleaned, metrics, explicit_metrics = _strip_metrics(raw_argv)
        final_artifact = Path(_option_value(cleaned, "--artifact-dir")).expanduser()
    except ValueError as exc:
        _core._build_parser().error(str(exc))
    if final_artifact.exists():
        _core._build_parser().error(f"artifact directory already exists: {final_artifact}")
    final_artifact.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_artifact.parent / (
        f".{final_artifact.name}.diagnosis-closure-{uuid.uuid4().hex}"
    )
    rewritten = _replace_option(cleaned, "--artifact-dir", str(temporary))
    rewritten.extend(["--metric", metrics[0]])
    parsed = _core._build_parser().parse_args(rewritten)

    with _patched_core_impl(
        metrics,
        explicit_metrics=explicit_metrics,
        surface=globals(),
    ):
        exit_code = _core.main(rewritten)
    if not temporary.is_dir():
        return exit_code
    try:
        enrich_completed_run(
            temporary,
            args=parsed,
            metrics=metrics,
            explicit_metrics=explicit_metrics,
            surface=globals(),
        )
    except Exception as exc:
        publish_enrichment_failure(temporary, exc)
        print(
            f"DIAGNOSIS_CLOSURE_FAILED error={_core._error_text(exc)}",
            file=sys.stderr,
        )
    _core._os_replace(temporary, final_artifact)
    print(f"DIAGNOSIS_ARTIFACTS {final_artifact}", flush=True)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    try:
        return _main_impl(argv)
    finally:
        _core.transport._release_active_remote_lock()


def __getattr__(name: str) -> Any:
    return getattr(_core, name)


if __name__ == "__main__":
    raise SystemExit(main())
