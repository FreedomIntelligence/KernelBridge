#!/usr/bin/env python3
"""逐算子的**流水占用率**剖析 —— 回答「这个 kernel 里哪一部分慢」。

    python3 pipeprof.py <Op> <srcdir> [--dev N] [--metric PipeUtilization]
        [--arm base|mt --case-id N --run-id ID --json-out report.json]

## 为什么需要它
官方评测把硬件计数器**关掉**了（`mc2_distributed_runner.py:566`、`perf_eval.py:85`
都写死 `aic_metrics=AiCMetrics.AiCoreNone`），所以评测报告只给 `elapsed_us`
——「整个 kernel 多久」，不给「里面哪部分慢」。
本战役已有四次按「能省多少次操作」定价而高估的事故
（92.46→1.71、15.1→0、17.26→0.27）：**根因都是不知道那部分占多少时间。**

## 做法
不改共享 harness（它是提交口径的一部分），而是在**子进程里**把
`torch_npu.profiler._ExperimentalConfig` 猴补丁掉，强制 `aic_metrics=<metric>`，
再照常调 harness 的 `run_evaluation.sh`。case 加载 / 反作弊 / 计时路径全部原样。

## ⚠ 读数纪律
计数器采样**本身有开销**，会让 kernel 变慢
⇒ **只能用来定位瓶颈，绝不能用来量 A/B 的 Δ**（那个仍然用 `mincase.py` 的 Eq.3 口径）。
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

def build_sitecustomize(metric: str, level: str) -> str:
    return textwrap.dedent(f'''
        # 由 pipeprof.py 生成：把评测写死的 AiCoreNone 换成真计数器
        import json as _j, os as _o
        _requested_metric = {metric!r}
        _requested_level = {level!r}

        def _pipeprof_emit(**_fields):
            _status = dict(
                requested_metric=_requested_metric,
                requested_level=_requested_level,
                pid=_o.getpid(),
            )
            _status.update(_fields)
            _marker = _o.environ.get("PIPEPROF_PATCH_MARKER")
            if not _marker:
                return
            try:
                # One compact O_APPEND write keeps records from evaluator
                # subprocesses from overwriting one another.
                _payload = (_j.dumps(_status, sort_keys=True) + "\\n").encode("utf-8")
                _fd = _o.open(_marker, _o.O_APPEND | _o.O_CREAT | _o.O_WRONLY, 0o600)
                try:
                    _o.write(_fd, _payload)
                finally:
                    _o.close(_fd)
            except Exception as _marker_error:
                import sys as _s
                print("[pipeprof] marker failed: %r" % (_marker_error,), file=_s.stderr)

        try:
            import torch_npu.profiler as _P
            import torch_npu.profiler.experimental_config as _E
            _orig = _E._ExperimentalConfig
            _M = getattr(_P.AiCMetrics, {metric!r})
            _L = getattr(_P.ProfilerLevel, {level!r})
            class _Patched(_orig):
                def __init__(self, *a, **kw):
                    # _ExperimentalConfig accepts profiler_level and
                    # aic_metrics as its first two positional parameters too.
                    # Replace either spelling without creating duplicate args.
                    _args = list(a)
                    if len(_args) >= 1:
                        _args[0] = _L
                        kw.pop("profiler_level", None)
                    else:
                        kw["profiler_level"] = _L
                    if len(_args) >= 2:
                        _args[1] = _M
                        kw.pop("aic_metrics", None)
                    else:
                        kw["aic_metrics"] = _M
                    super().__init__(*_args, **kw)
                    _effective_metric = getattr(self, "_aic_metrics", None)
                    _effective_level = getattr(self, "_profiler_level", None)
                    _ok = _effective_metric == _M and _effective_level == _L
                    _pipeprof_emit(
                        event="constructed",
                        applied=_ok,
                        effective_metric=str(_effective_metric),
                        effective_level=str(_effective_level),
                    )
                    if not _ok:
                        raise RuntimeError(
                            "pipeprof ExperimentalConfig mismatch: %r/%r"
                            % (_effective_metric, _effective_level)
                        )
            _P._ExperimentalConfig = _Patched
            _E._ExperimentalConfig = _Patched
            # profiler.py imports the class as an alias in this torch_npu
            # release. Patch it as well so every supported call path agrees.
            try:
                import torch_npu.profiler.profiler as _PP
                if getattr(_PP, "_ExperimentalConfig", None) is _orig:
                    _PP._ExperimentalConfig = _Patched
            except Exception:
                pass
            _pipeprof_emit(event="installed", applied=True)
            import sys as _s; print("[pipeprof] aic_metrics={metric} level={level}", file=_s.stderr)
        except Exception as _e:
            _pipeprof_emit(event="install_error", applied=False, error=repr(_e))
            import sys as _s; print("[pipeprof] patch failed: %r" % (_e,), file=_s.stderr)
    ''').strip()

METRIC_COLS = {
    # 各组里真正回答「谁是瓶颈」的列（大小写不敏感子串匹配）
    "PipeUtilization": ["vec_ratio","mac_ratio","scalar_ratio","mte1_ratio","mte2_ratio",
                        "mte3_ratio","fixpipe_ratio","aiv_vec_ratio","aiv_scalar_ratio",
                        "aiv_mte2_ratio","aiv_mte3_ratio"],
    "MemoryUB":        ["ub_read","ub_write","ub_"],
    "Memory":          ["read_bw","write_bw","bandwidth"],
    "MemoryL0":        ["l0a", "l0b", "l0c", "l1_read", "l1_write", "l1_"],
    "ResourceConflictRatio": ["bank_cflt", "bankgroup_cflt", "resc_cflt", "conflict"],
    "L2Cache":         ["l2_", "cache_hit", "cache_miss", "victim"],
    "MemoryAccess":    ["_datas", "memory_data", "gm_to_", "to_gm", "main_memory"],
    "ArithmeticUtilization": ["ratio"],
    "AiCoreNone":       [],
}

AIC_METRIC_VALUES = {
    "AiCoreNone": "ACL_AICORE_NONE",
    "ArithmeticUtilization": "ACL_AICORE_ARITHMETIC_UTILIZATION",
    "L2Cache": "ACL_AICORE_L2_CACHE",
    "Memory": "ACL_AICORE_MEMORY_BANDWIDTH",
    "MemoryAccess": "ACL_AICORE_MEMORY_ACCESS",
    "MemoryL0": "ACL_AICORE_L0B_AND_WIDTH",
    "MemoryUB": "ACL_AICORE_MEMORY_UB",
    "PipeUtilization": "ACL_AICORE_PIPE_UTILIZATION",
    "ResourceConflictRatio": "ACL_AICORE_RESOURCE_CONFLICT_RATIO",
}

CONTEXT_COLUMNS = {
    "duration_us",
    "wait_time_us",
    "block_num",
    "mix_block_num",
    "accelerator_core",
}
HARNESS_KERNEL_MARKERS = ("cannbenchcacheclean", "cann_bench_cache_clean")


class CaptureError(RuntimeError):
    def __init__(self, message, exit_code=2, stdout="", stderr=""):
        super().__init__(message)
        self.exit_code = int(exit_code)
        self.stdout = stdout or ""
        self.stderr = stderr or ""


def _snake_name(name):
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", str(name))
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def prepare_isolated_task(bench, operator, case_id, work, task_rel=None):
    """Copy one harness task and retain exactly one requested case."""
    import yaml

    tasks_root = (Path(bench) / "tasks").resolve()
    if task_rel:
        source = (tasks_root / task_rel).resolve()
        if source != tasks_root and tasks_root not in source.parents:
            raise CaptureError("--task-rel escapes the harness tasks directory")
        candidates = [source]
    else:
        wanted = _snake_name(operator)
        candidates = sorted(
            path.parent
            for path in tasks_root.glob("**/cases.yaml")
            if _snake_name(path.parent.name) == wanted
        )
    candidates = [path for path in candidates if (path / "cases.yaml").is_file()]
    if len(candidates) != 1:
        rels = [str(path.relative_to(tasks_root)).replace("\\", "/") for path in candidates]
        raise CaptureError(
            "cannot resolve one task for %s; matches=%s (use --task-rel)" % (operator, rels)
        )
    source = candidates[0]
    relative = source.relative_to(tasks_root)
    isolated_root = Path(work) / "task"
    target = isolated_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    cases_path = target / "cases.yaml"
    payload = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    cases = payload.get("cases") or []
    selected = [row for row in cases if str(row.get("case_id")) == str(case_id)]
    if len(selected) != 1:
        raise CaptureError(
            "case_id %s not found exactly once in %s" % (case_id, source / "cases.yaml")
        )
    payload["cases"] = selected
    replacement = cases_path.with_suffix(".tmp")
    replacement.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    os.replace(replacement, cases_path)
    return isolated_root, str(relative).replace("\\", "/")


def create_fresh_output_dir(path):
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise CaptureError("refusing existing profiler output directory: %s" % path) from exc
    return path


def default_output_dir(operator, metric, run_id, arm=None, case_id=None):
    """Return a collision-free default for different arms in one A/B run."""
    parts = (
        _snake_name(operator) or "op",
        _snake_name(arm) or "unpaired",
        "case%s" % case_id if case_id is not None else "all_cases",
        _snake_name(metric) or "metric",
        _snake_name(run_id) or "run",
    )
    return "/tmp/pipeprof_%s" % "_".join(parts)


def execute_harness(cmd, env, cwd, runner=None):
    runner = runner or subprocess.run
    result = runner(cmd, env=env, cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise CaptureError(
            "profiler harness failed with exit %s" % result.returncode,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result


def prepare_harness_overlay(bench, work, patch_source):
    """Copy the small harness control plane and inject our sitecustomize there.

    ``run_evaluation.sh`` deliberately prepends ``$PROJECT_ROOT/src`` to
    PYTHONPATH.  A sitecustomize placed elsewhere can therefore be shadowed by
    the harness' own ``src/sitecustomize.py``.  Use a disposable overlay so the
    first sitecustomize on the real evaluator path contains the PMU patch while
    the shared harness remains byte-for-byte untouched.
    """
    bench = Path(bench).resolve()
    overlay = Path(work) / "harness_overlay"
    for directory in ("src", "scripts"):
        source = bench / directory
        if not source.is_dir():
            raise CaptureError("harness is missing %s" % source)
        shutil.copytree(source, overlay / directory, symlinks=True)
    for filename in ("VERSION", "pyproject.toml"):
        source = bench / filename
        if source.is_file():
            shutil.copy2(source, overlay / filename)

    tasks = bench / "tasks"
    if tasks.is_dir():
        try:
            os.symlink(str(tasks), str(overlay / "tasks"), target_is_directory=True)
        except OSError as exc:
            raise CaptureError("cannot link harness tasks into overlay: %s" % exc) from exc

    sitecustomize = overlay / "src" / "sitecustomize.py"
    existing = sitecustomize.read_text(encoding="utf-8") if sitecustomize.is_file() else ""
    sitecustomize.write_text(
        existing.rstrip() + "\n\n" + patch_source.rstrip() + "\n",
        encoding="utf-8",
    )
    return overlay


def verify_patch_marker(path, metric, level):
    path = Path(path)
    statuses = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                statuses.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    expected_metric = AIC_METRIC_VALUES[metric]
    constructed = [
        row for row in statuses
        if row.get("event") == "constructed"
        and row.get("requested_metric") == metric
        and row.get("requested_level") == level
    ]
    verified = [
        row for row in constructed
        if row.get("applied") is True
        and row.get("effective_metric") == expected_metric
        and row.get("effective_level") == level
    ]
    if not verified:
        errors = [row.get("error") for row in statuses if row.get("error")]
        raise CaptureError(
            "profiler monkey patch constructor was not verified for %s/%s; "
            "constructed=%s errors=%s" % (metric, level, len(constructed), errors)
        )
    mismatched = [row for row in constructed if row not in verified]
    if mismatched:
        raise CaptureError(
            "profiler monkey patch constructed a mismatched config for %s/%s: %s"
            % (metric, level, mismatched)
        )
    return statuses


def verify_profiler_info(prof_dir, metric, level):
    """Require archived profiler metadata to agree with the requested PMU."""
    files = sorted(Path(prof_dir).glob("**/profiler_info.json"))
    if not files:
        raise CaptureError("profiler_info.json missing; cannot verify PMU capture")
    expected_metric = AIC_METRIC_VALUES[metric]
    records = []
    errors = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            config = payload["config"]["experimental_config"]
            records.append(
                {
                    "path": os.path.relpath(path, prof_dir).replace("\\", "/"),
                    "aic_metrics": config.get("_aic_metrics"),
                    "profiler_level": config.get("_profiler_level"),
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append("%s: %r" % (path, exc))
    if not records:
        raise CaptureError("no readable profiler_info config; errors=%s" % errors)
    mismatched = [
        row for row in records
        if row["aic_metrics"] != expected_metric or row["profiler_level"] != level
    ]
    if mismatched:
        raise CaptureError(
            "profiler_info does not prove %s/%s; observed=%s"
            % (expected_metric, level, mismatched)
        )
    return records


def parse_numeric(value):
    """Parse profiler numbers while preserving the human-facing percent scale."""
    if value in (None, "", "N/A", "n/a", "NA", "-"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    match = re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text)
    if not match:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    frac = pos - lo
    return values[lo] * (1.0 - frac) + values[hi] * frac


def _stats(values):
    values = list(values)
    if not values:
        return None
    p50 = _percentile(values, 0.50)
    result = {
        "n": len(values),
        "mean": sum(values) / len(values),
        "p50": p50,
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "mad": _percentile([abs(value - p50) for value in values], 0.50),
        "min": min(values),
        "max": max(values),
    }
    result["p95_p50_ratio"] = result["p95"] / p50 if p50 not in (None, 0.0) else None
    return result


def _profile_csv_files(prof_dir):
    files = glob.glob(os.path.join(prof_dir, "**", "kernel_details.csv"), recursive=True)
    if not files:
        files = glob.glob(os.path.join(prof_dir, "**", "op_summary*.csv"), recursive=True)
    return sorted(files)


def _column_matches(column, pattern):
    normalized = _snake_name(column)
    if pattern == "ratio":
        return normalized == "ratio" or normalized.endswith("_ratio") or "_ratio_" in normalized
    return pattern in normalized


def _is_harness_kernel(name):
    normalized = re.sub(r"[^a-z0-9]+", "", str(name).lower())
    return any(marker.replace("_", "") in normalized for marker in HARNESS_KERNEL_MARKERS)


def _context_stats(rows, headers):
    result = {}
    for column in headers:
        if _snake_name(column) not in CONTEXT_COLUMNS:
            continue
        values = [parse_numeric(row.get(column)) for row in rows]
        stats = _stats(value for value in values if value is not None)
        if stats is not None:
            result[column] = stats
    return result


def summarize_structured(prof_dir, metric, metadata=None):
    """Return a versioned, loss-aware JSON summary for one metric capture."""
    files = _profile_csv_files(prof_dir)
    if not files:
        return None, "没找到 kernel_details.csv / op_summary*.csv"

    all_rows_by_kernel = collections.defaultdict(list)
    headers = []
    for path in files:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            for row in csv.DictReader(fh):
                for key in row:
                    if key not in headers:
                        headers.append(key)
                name = (row.get("Name") or row.get("Op Name") or row.get("op_name") or "?").strip()
                all_rows_by_kernel[name].append(row)

    excluded_kernels = {
        name: len(rows) for name, rows in all_rows_by_kernel.items() if _is_harness_kernel(name)
    }
    rows_by_kernel = {
        name: rows for name, rows in all_rows_by_kernel.items() if name not in excluded_kernels
    }
    target_rows = [row for rows in rows_by_kernel.values() for row in rows]

    patterns = METRIC_COLS.get(metric, [])
    selected = []
    for column in headers:
        lower = column.lower()
        if not any(_column_matches(column, pattern) for pattern in patterns):
            continue
        if any(parse_numeric(row.get(column)) is not None for row in target_rows):
            selected.append(column)

    duration_column = next(
        (column for column in headers if _snake_name(column) == "duration_us"), None
    )
    wait_column = next(
        (column for column in headers if _snake_name(column) == "wait_time_us"), None
    )

    kernels = {}
    for name in sorted(rows_by_kernel):
        rows = rows_by_kernel[name]
        metrics = {}
        for column in selected:
            values = [parse_numeric(row.get(column)) for row in rows]
            stats = _stats(value for value in values if value is not None)
            if stats is not None:
                metrics[column] = stats
        kernels[name] = {
            "launch_count": len(rows),
            "metrics": metrics,
            "context_metrics": _context_stats(rows, headers),
        }

    duration_by_kernel = {}
    if duration_column:
        for name, rows in rows_by_kernel.items():
            duration_by_kernel[name] = sum(
                value
                for value in (parse_numeric(row.get(duration_column)) for row in rows)
                if value is not None and value >= 0
            )
        total_duration = sum(duration_by_kernel.values())
        for name, duration in duration_by_kernel.items():
            kernels[name]["duration_us_sum"] = duration
            kernels[name]["duration_share"] = (
                duration / total_duration if total_duration > 0 else None
            )

    aggregate_metrics = {}
    all_rows = target_rows
    for column in selected:
        values = [parse_numeric(row.get(column)) for row in all_rows]
        stats = _stats(value for value in values if value is not None)
        if stats is not None:
            aggregate_metrics[column] = stats

    duration_weighted_metrics = {}
    if duration_column:
        for column in selected:
            normalized = _snake_name(column)
            # Only ratio/rate-like fields have a well-defined weighted-mean
            # aggregation.  Bytes/counts need sums and hit/miss pairs need a
            # reconstructed ratio, so unknown semantics remain per-kernel.
            if not (
                normalized.endswith("_ratio")
                or "_ratio_" in normalized
                or "bandwidth" in normalized
                or normalized.endswith("_bw")
            ):
                continue
            kernel_values = []
            for item in kernels.values():
                value = (item.get("metrics", {}).get(column) or {}).get("p50")
                duration = (
                    item.get("context_metrics", {}).get(duration_column) or {}
                ).get("p50")
                launches = item.get("launch_count") or 0
                if value is not None and duration is not None and duration >= 0 and launches:
                    kernel_values.append((value, duration * launches))
            weight = sum(duration for _, duration in kernel_values)
            if kernel_values and weight > 0:
                duration_weighted_metrics[column] = {
                    "duration_weighted_mean": sum(
                        value * duration for value, duration in kernel_values
                    ) / weight,
                    "aggregation": "kernel_p50 weighted by kernel_duration_p50 * launch_count",
                    "kernel_count": len(kernel_values),
                    "weight_us": weight,
                }

    context_metrics = _context_stats(all_rows, headers)
    derived_metrics = {}
    for label, column in (("kernel_duration", duration_column), ("kernel_wait", wait_column)):
        if not column:
            continue
        for stat in ("p50", "p95", "p99"):
            values = [
                item["context_metrics"].get(column, {}).get(stat)
                for item in kernels.values()
            ]
            values = [value for value in values if value is not None]
            if values:
                derived_metrics["sum_%s_%s_us" % (label, stat)] = sum(values)

    no_pmu_control = metric == "AiCoreNone"
    if not rows_by_kernel:
        unavailable_reason = "no target kernel rows after harness-kernel exclusion"
    elif no_pmu_control and "sum_kernel_duration_p50_us" not in derived_metrics:
        unavailable_reason = "no-PMU control has no numeric Duration(us) column"
    elif not selected:
        unavailable_reason = None if no_pmu_control else "no matching numeric columns in profiler output"
    else:
        unavailable_reason = None

    report = {
        "schema_version": "pipeprof/v1",
        "metric_group": metric,
        # AiCoreNone still traverses the profiler/report path.  It is useful as
        # an overhead control, but is never authoritative profiler-off timing.
        "capture_kind": "no_pmu_control" if no_pmu_control else "pmu",
        "timing_authority": False,
        "available": bool(rows_by_kernel) and (
            bool(selected) or (no_pmu_control and unavailable_reason is None)
        ),
        "unavailable_reason": unavailable_reason,
        "selected_columns": selected,
        "available_columns": headers,
        "source_files": [os.path.relpath(path, prof_dir).replace("\\", "/") for path in files],
        "all_kernel_count": len(all_rows_by_kernel),
        "kernel_count": len(kernels),
        "launch_count": sum(item["launch_count"] for item in kernels.values()),
        "excluded_kernels": excluded_kernels,
        "excluded_launch_count": sum(excluded_kernels.values()),
        "context_metrics": context_metrics,
        "derived_metrics": derived_metrics,
        "aggregate_metrics": aggregate_metrics,
        "duration_weighted_metrics": duration_weighted_metrics,
        "kernels": kernels,
    }
    if metadata:
        report["metadata"] = dict(metadata)
    return report, None

def summarize(prof_dir: str, metric: str):
    """从 profiler 产物里挑出 kernel_details.csv，按 kernel 名聚合各流水占用率。"""
    files = _profile_csv_files(prof_dir)
    if not files:
        return None, "没找到 kernel_details.csv / op_summary*.csv"
    agg = collections.defaultdict(lambda: collections.defaultdict(list))
    hdr = None
    for f in files:
        with open(f, newline="", encoding="utf-8-sig", errors="replace") as fh:
            for row in csv.DictReader(fh):
                hdr = hdr or list(row)
                name = (row.get("Name") or row.get("Op Name") or row.get("op_name") or "?").strip()
                for k, v in row.items():
                    if v in (None, "", "N/A"): continue
                    fv = parse_numeric(v)
                    if fv is None: continue
                    agg[name][k].append(fv)
    return (agg, hdr), None

def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operator")
    parser.add_argument("source_dir")
    parser.add_argument("--dev", default="6")
    parser.add_argument("--metric", default="PipeUtilization", choices=sorted(METRIC_COLS))
    parser.add_argument("--level", default="Level1")
    parser.add_argument("--out")
    parser.add_argument("--json-out")
    parser.add_argument("--bench")
    parser.add_argument("--arm")
    parser.add_argument("--case-id", type=int)
    parser.add_argument("--task-rel")
    parser.add_argument("--run-id")
    parser.add_argument("--soc")
    parser.add_argument("--cann-version")
    parser.add_argument("--compiler-version")
    parser.add_argument("--dtype")
    parser.add_argument("--shape-domain")
    return parser


def missing_ab_metadata(args):
    if not getattr(args, "arm", None):
        return []
    required = (
        "case_id",
        "run_id",
        "soc",
        "cann_version",
        "compiler_version",
        "dtype",
        "shape_domain",
    )
    return [name for name in required if getattr(args, name, None) in (None, "")]


def main(argv=None):
    args = _parser().parse_args(argv)
    op, srcdir = args.operator, args.source_dir
    dev, metric, level = args.dev, args.metric, args.level
    run_id = args.run_id or "%s-%s" % (time.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8])
    outdir = args.out or default_output_dir(
        op, metric, run_id, arm=args.arm, case_id=args.case_id
    )
    json_out = args.json_out

    missing = missing_ab_metadata(args)
    if missing:
        print(
            "[pipeprof] paired --arm capture requires metadata: %s"
            % ", ".join("--" + name.replace("_", "-") for name in missing),
            file=sys.stderr,
        )
        return 2

    if args.task_rel and args.case_id is None:
        print("[pipeprof] --task-rel requires --case-id", file=sys.stderr)
        return 2
    if json_out and Path(json_out).exists():
        print("[pipeprof] refusing existing JSON output: %s" % json_out, file=sys.stderr)
        return 2

    # ⚠ 不能用 os.path.expanduser("~")：脚本常以 root 跑而 harness 在 /home/demo1
    #    （现场：解析成 /root/cann-bench-20260722 -> FileNotFoundError）
    bench = args.bench
    if not bench:
        for cand in ("/home/demo1/cann-bench-20260722", "/root/cann-bench-20260722",
                      os.path.expanduser("~/cann-bench-20260722")):
            if os.path.isdir(cand): bench = cand; break
    if not bench or not os.path.isdir(bench):
        print("[pipeprof] 找不到 harness 目录，用 --bench 指定", file=sys.stderr)
        return 2
    print("[pipeprof] bench=%s" % bench)

    try:
        with tempfile.TemporaryDirectory(prefix="pipeprof_") as work_text:
            work = Path(work_text)
            harness_overlay = prepare_harness_overlay(
                bench, work, build_sitecustomize(metric, level)
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = (
                str(harness_overlay / "src")
                + os.pathsep
                + env.get("PYTHONPATH", "")
            )
            patch_marker = work / "patch_status.jsonl"
            env["PIPEPROF_PATCH_MARKER"] = str(patch_marker)

            task_root = None
            task_rel = None
            if args.case_id is not None:
                task_root, task_rel = prepare_isolated_task(
                    bench, op, args.case_id, work, task_rel=args.task_rel
                )
            create_fresh_output_dir(outdir)
            cmd = ["./scripts/run_evaluation.sh", "--source-dir", srcdir,
                   "--operator", op, "--device", "npu", "--device-id", str(dev),
                   "--reports-dir", outdir, "--profiler-level", level]
            if task_root is not None:
                cmd.extend(["--task-dir", str(task_root)])
            print("[pipeprof] metric=%s level=%s dev=%s\n[pipeprof] %s" %
                  (metric, level, dev, " ".join(cmd)))
            result = execute_harness(cmd, env, harness_overlay)
            patch_statuses = verify_patch_marker(patch_marker, metric, level)
            profiler_info = verify_profiler_info(outdir, metric, level)
            print(result.stdout[-2500:])

            constructed = [
                row for row in patch_statuses if row.get("event") == "constructed"
            ]

            metadata = {
                "operator": op,
                "source_dir": srcdir,
                "device": str(dev),
                "profiler_level": level,
                "arm": args.arm,
                "case_id": args.case_id,
                "task_rel": task_rel,
                "run_id": run_id,
                "soc": args.soc,
                "cann_version": args.cann_version,
                "compiler_version": args.compiler_version,
                "dtype": args.dtype,
                "shape_domain": args.shape_domain,
                "harness_returncode": result.returncode,
                "patch_verified": True,
                "patch_constructor_calls": len(constructed),
                "patch_processes": len(
                    {row.get("pid") for row in constructed if row.get("pid") is not None}
                ),
                "profiler_info_verified": True,
                "profiler_info": profiler_info,
            }
            metadata = {key: value for key, value in metadata.items() if value is not None}
            report, err = summarize_structured(outdir, metric, metadata=metadata)
    except CaptureError as exc:
        if exc.stdout:
            print(exc.stdout[-2500:])
        if exc.stderr:
            print("STDERR:", exc.stderr[-1500:], file=sys.stderr)
        print("[pipeprof] %s" % exc, file=sys.stderr)
        return exc.exit_code if 0 < exc.exit_code < 126 else 2

    if err:
        print("[pipeprof] %s" % err, file=sys.stderr)
        return 4
    if json_out:
        os.makedirs(os.path.dirname(os.path.abspath(json_out)), exist_ok=True)
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        print("[pipeprof] JSON %s" % json_out)
    cols = report["selected_columns"]
    value_section = "metrics"
    if not cols and report.get("capture_kind") == "no_pmu_control":
        cols = [
            column for column in report["context_metrics"]
            if _snake_name(column) in {"duration_us", "wait_time_us"}
        ]
        value_section = "context_metrics"
    if not report["available"]:
        print("[pipeprof] %s unavailable: %s" % (metric, report["unavailable_reason"]))
    print("\n=== 逐 kernel 的%s（p50；JSON 同时保留 mean/p95/min/max）===" % metric)
    print("%-52s %8s  %s" % ("kernel", "n", "  ".join("%-14s" % c[:14] for c in cols)))
    for name, item in sorted(report["kernels"].items(), key=lambda kv: -kv[1]["launch_count"]):
        n = item["launch_count"]
        vals = []
        for c in cols:
            stats = item[value_section].get(c)
            vals.append("%-14.4f" % stats["p50"] if stats else "%-14s" % "-")
        print("%-52s %8d  %s" % (name[:52], n, "  ".join(vals)))
    print("\n⚠ 计数器采样本身有开销 ⇒ 只用于**定位瓶颈**，量 Δ 仍用 mincase.py 的 Eq.3 口径")
    return 0 if report["available"] else 5

if __name__ == "__main__":
    raise SystemExit(main())
