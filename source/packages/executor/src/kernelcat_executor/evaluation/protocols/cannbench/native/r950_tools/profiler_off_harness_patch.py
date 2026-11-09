#!/usr/bin/env python3
"""Patch an isolated CANNBench copy to time calls with raw NPU Events.

This never edits the shared harness.  The target must be an isolated copy of
``src/kernel_eval/eval/perf_eval.py`` under a unique remote ``/tmp`` run.
"""
from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "# PROFILER_OFF_EVENT_PATCH_V1"
PATCH = r'''

# PROFILER_OFF_EVENT_PATCH_V1
def _profiler_off_event_profile(self, fn, prof_dir, warmup, repeat, include_host=False):
    """Experiment-only replacement for profiler collection using NPU Events."""
    import csv as _csv
    import json as _json
    import os as _os
    import torch_npu as _torch_npu

    inner_repeat = int(_os.environ.get("PROFILER_AB_INNER_REPEAT", "1"))
    if inner_repeat < 1 or inner_repeat > 1000:
        raise ValueError("PROFILER_AB_INNER_REPEAT must be in [1, 1000]")
    cache_clean_each_sample = (
        bool(self.freq_boost)
        and _os.environ.get("PROFILER_AB_CACHE_CLEAN_EACH_SAMPLE", "1") != "0"
    )

    # Preserve the established pre-flight/warmup/cache discipline, but place
    # no torch_npu.profiler context around the measured call.
    fn()
    _torch_npu.npu.synchronize()
    for _ in range(warmup):
        fn()
        _torch_npu.npu.synchronize()

    samples_us = []
    for _ in range(repeat):
        if cache_clean_each_sample:
            self._clear_cache()
        start = _torch_npu.npu.Event(enable_timing=True)
        end = _torch_npu.npu.Event(enable_timing=True)
        start.record()
        for _inner in range(inner_repeat):
            fn()
        end.record()
        _torch_npu.npu.synchronize()
        # torch_npu Event elapsed_time is milliseconds.
        samples_us.append(float(start.elapsed_time(end)) * 1000.0 / inner_repeat)

    _os.makedirs(prof_dir, exist_ok=True)
    csv_path = _os.path.join(prof_dir, "kernel_details.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(
            fh, fieldnames=["Step Id", "Name", "Type", "Duration(us)"]
        )
        writer.writeheader()
        for index, value in enumerate(samples_us):
            writer.writerow(
                {
                    "Step Id": index,
                    "Name": "ProfilerOffEventTotal",
                    "Type": "ProfilerOffEventTotal",
                    "Duration(us)": "%.9f" % value,
                }
            )

    metadata_fields = (
        "operator", "arm", "case_id", "run_id", "soc", "cann_version",
        "compiler_version", "dtype", "shape_domain", "device", "sequence",
    )
    metadata = {
        field: _os.environ.get("PROFILER_AB_" + field.upper())
        for field in metadata_fields
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, "")}
    metadata.update(
        {
            "warmup": warmup,
            "repeat": repeat,
            "inner_repeat": inner_repeat,
            "cache_clean_each_sample": cache_clean_each_sample,
            "timing_scope": (
                "single_call"
                if inner_repeat == 1
                else (
                    "batched_cold_start_amortized_per_call"
                    if cache_clean_each_sample
                    else "batched_steady_state_per_call"
                )
            ),
        }
    )
    report = {
        "schema_version": "profiler-off-timing/v1",
        "profiler_enabled": False,
        "timer": "npu_event",
        "unit": "us",
        "samples_us": samples_us,
        "metadata": metadata,
    }
    temporary = _os.path.join(prof_dir, "event_samples.tmp")
    with open(temporary, "w", encoding="utf-8") as fh:
        _json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    _os.replace(temporary, _os.path.join(prof_dir, "event_samples.json"))


PerfEvaluator._profile = _profiler_off_event_profile
'''


def apply_patch(target: Path) -> bool:
    text = target.read_text(encoding="utf-8")
    if MARKER in text:
        return False
    target.write_text(text.rstrip() + PATCH + "\n", encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    args = parser.parse_args(argv)
    if not args.target.is_file() or args.target.name != "perf_eval.py":
        parser.error("target must be an isolated perf_eval.py")
    changed = apply_patch(args.target)
    print("patched" if changed else "already patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
