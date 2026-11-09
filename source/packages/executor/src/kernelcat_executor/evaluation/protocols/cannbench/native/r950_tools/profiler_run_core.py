#!/usr/bin/env python3
"""Run one failure-tolerant same-card 910C Event + directed-PMU A/B profile.

Stage 1 keeps the existing DevHost/SSH execution protocol authoritative while
making completion deterministic: every attempted run collects best-effort
artifacts, emits a full audit view and a compact agent view, records monotonic
stage timings, and publishes the final directory with one atomic rename.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import profiler_transport as transport
from profiler_agent_projection import (
    DEFAULT_TOKEN_BUDGET,
    build_agent_projection,
    serialize_projection,
)
from profiler_feedback_sdk import query_completed_run, serialize_feedback

RUN_SCHEMA_VERSION = "profiler-run/v2"
ATTEMPT_SCHEMA_VERSION = "profiler-attempt-event/v1"
SUMMARY_SCHEMA_VERSION = "profiler-tournament-summary/v1"
PAIR_SCHEMA_VERSION = "real-profiler-ab-run/v1"
BACKEND_NAME = "devhost-ssh"
RUNNER_NAME = "r950_tools/profiler_run.py"


@dataclass
class StageResult:
    ok: bool
    value: Any = None
    error: str | None = None
    remote_elapsed_s: float | None = None


@dataclass
class RunContext:
    run_id: str
    experiment_id: str
    case_id: int
    device: int
    attempts: list[dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}_{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _error_text(exc: BaseException | str) -> str:
    if isinstance(exc, BaseException):
        text = f"{type(exc).__name__}: {exc}"
    else:
        text = str(exc)
    return text.replace("\x00", "")[:4000]


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        pass


def _os_replace(source: Path, dest: Path) -> None:
    """Atomic rename with a Windows fallback when the destination is briefly locked."""
    try:
        os.replace(source, dest)
        return
    except PermissionError:
        if source.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            try:
                os.replace(source, dest)
                return
            except PermissionError:
                shutil.copytree(source, dest, dirs_exist_ok=True)
                shutil.rmtree(source, ignore_errors=True)
                return
        dest.write_bytes(source.read_bytes())
        _unlink_quietly(source)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        _os_replace(temporary, path)
    finally:
        _unlink_quietly(temporary)


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, compact: bool = False) -> None:
    if compact:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        _os_replace(temporary, target)
    finally:
        _unlink_quietly(temporary)


def _attempt_id(payload: Mapping[str, Any]) -> str:
    stable = {
        key: payload.get(key)
        for key in (
            "run_id",
            "experiment_id",
            "stage",
            "arm",
            "case_id",
            "started_at",
        )
    }
    return _content_id("attempt", stable)


def _record_attempt(
    context: RunContext,
    *,
    stage: str,
    status: str,
    started_at: str,
    elapsed_s: float,
    arm: str | None = None,
    error: str | None = None,
    remote_elapsed_s: float | None = None,
    artifact_refs: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "run_id": context.run_id,
        "experiment_id": context.experiment_id,
        "kind": "profiler_stage",
        "stage": stage,
        "status": status,
        "arm": arm,
        "case_id": context.case_id,
        "started_at": started_at,
        "elapsed_s": round(max(0.0, elapsed_s), 6),
        "remote_elapsed_s": (
            round(max(0.0, remote_elapsed_s), 6)
            if remote_elapsed_s is not None
            else None
        ),
        "error": error,
        "artifact_refs": list(artifact_refs or []),
        "provenance": {
            "runner": RUNNER_NAME,
            "backend": BACKEND_NAME,
            "device": context.device,
        },
    }
    payload["event_id"] = _attempt_id(payload)
    context.attempts.append(payload)
    return payload


def _run_stage(
    context: RunContext,
    stage: str,
    function: Callable[[], Any],
    *,
    arm: str | None = None,
) -> StageResult:
    print(stage if arm is None else f"{stage} {arm}", flush=True)
    started_at = _utc_now()
    started = time.monotonic()
    try:
        raw = function()
        result = raw if isinstance(raw, StageResult) else StageResult(ok=True, value=raw)
    except Exception as exc:
        result = StageResult(ok=False, error=_error_text(exc))
    elapsed = time.monotonic() - started
    status = "succeeded" if result.ok else "failed"
    _record_attempt(
        context,
        stage=stage,
        status=status,
        started_at=started_at,
        elapsed_s=elapsed,
        arm=arm,
        error=result.error,
        remote_elapsed_s=result.remote_elapsed_s,
    )
    if not result.ok:
        print(
            f"{stage}_FAILED" + (f" arm={arm}" if arm else "") + f" error={result.error}",
            flush=True,
        )
    return result


def _skip_stage(
    context: RunContext,
    stage: str,
    reason: str,
    *,
    arm: str | None = None,
) -> None:
    _record_attempt(
        context,
        stage=stage,
        status="skipped",
        started_at=_utc_now(),
        elapsed_s=0.0,
        arm=arm,
        error=reason,
    )


def _validate_component(value: str, option: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError(f"{option} must be one safe [A-Za-z0-9_.-] path component")
    if value in {".", ".."}:
        raise ValueError(f"unsafe {option}: {value!r}")
    return value


def _experiment_identity(
    args: argparse.Namespace,
    *,
    source_hashes: Mapping[str, str],
    snake: str,
    metric: str,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "schema_version": "profiler-experiment-key/v1",
        "operator": args.operator,
        "task_rel": f"{args.level}/{snake}",
        "source_tree_sha256": dict(source_hashes),
        "case_ids": [args.case_id],
        "backend": BACKEND_NAME,
        "device_pool": [args.device],
        "event_protocol": {
            "warmup": args.warmup,
            "outer_samples": args.repeat,
            "inner_repeat": args.inner_repeat,
            "cache_mode": args.event_cache_mode,
            "stat": "p50",
        },
        "pmu_metric_groups": [metric],
        "action_cell": args.action_cell,
        "bench": args.bench,
        "toolchain": {
            "soc": args.soc,
            "cann_version": args.cann_version,
            "compiler_version": args.compiler_version,
            "dtype": args.dtype,
            "shape_domain": args.shape_domain,
        },
    }
    return _content_id("experiment", payload), payload


def _precheck(args: argparse.Namespace) -> tuple[object, str]:
    cfg = transport.load_config(args.school_env)
    output = transport._remote(
        "ps -eo pid,etime,command | grep -E '[r]un_evaluation|[p]ipeprof|[a]b20_' || true; "
        "npu-smi info",
        cfg,
        timeout=120,
    )
    text = transport._text(output)
    if "run_evaluation.sh" in text or "pipeprof.py" in text:
        raise RuntimeError("another evaluation is active; refusing to overlap")
    usages = transport._aicore_usages(text)
    if args.device not in usages:
        raise RuntimeError(f"could not read AICore utilization for device {args.device}")
    if usages[args.device] > 5:
        raise RuntimeError(
            f"device {args.device} is busy at {usages[args.device]}% AICore; refusing to overlap"
        )
    transport._acquire_remote_lock(cfg, device=args.device, owner=args.run_id)
    return cfg, text


def _upload(cfg, *, archive_path: str, payload: bytes) -> None:
    output = transport._remote(
        "umask 077; cat > " + shlex.quote(archive_path) + "; echo UPLOAD_OK",
        cfg,
        timeout=180,
        stdin=payload,
    )
    transport._require_ack(output, "UPLOAD_OK", "upload")


def _setup_remote(
    args: argparse.Namespace,
    cfg,
    *,
    remote_run: str,
    archive_path: str,
    task_rel: str,
) -> None:
    setup = f"""
set -euo pipefail
RUN={shlex.quote(remote_run)}
BENCH={shlex.quote(args.bench)}
test "$RUN" != /tmp
rm -rf "$RUN"
mkdir -p "$RUN"
tar xzf {shlex.quote(archive_path)} -C "$RUN"
mkdir -p "$RUN/bench_overlay"
cp -a "$BENCH/src" "$RUN/bench_overlay/src"
cp -a "$BENCH/scripts" "$RUN/bench_overlay/scripts"
cp "$BENCH/VERSION" "$RUN/bench_overlay/VERSION" 2>/dev/null || true
cp "$BENCH/pyproject.toml" "$RUN/bench_overlay/pyproject.toml" 2>/dev/null || true
mkdir -p "$RUN/task/{args.level}"
cp -a "$BENCH/tasks/{task_rel}" "$RUN/task/{task_rel}"
ln -s "$RUN/task" "$RUN/bench_overlay/tasks"
chmod -R u+w "$RUN"
python3 - "$RUN/task/{task_rel}/cases.yaml" {args.case_id} <<'PY_REMOTE_CASE'
import os, sys, yaml
from pathlib import Path
path = Path(sys.argv[1])
case_id = int(sys.argv[2])
data = yaml.safe_load(path.read_text())
selected = [row for row in data.get("cases", []) if int(row.get("case_id")) == case_id]
assert len(selected) == 1, (case_id, len(selected))
data["cases"] = selected
temporary = path.with_suffix(".tmp")
temporary.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
os.replace(temporary, path)
PY_REMOTE_CASE
python3 "$RUN/profiler_off_harness_patch.py" "$RUN/bench_overlay/src/kernel_eval/eval/perf_eval.py"
python3 - "$RUN/bench_overlay/src/cann_bench_utils/setup.py" <<'PY_REMOTE_SETUP'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
old = "cpu_count = os.cpu_count() or 2"
new = 'cpu_count = int(os.environ.get("CANN_BUILD_JOBS") or (os.cpu_count() or 2))'
assert text.count(old) == 1, text.count(old)
path.write_text(text.replace(old, new))
PY_REMOTE_SETUP
python3 -m venv --system-site-packages "$RUN/venv"
C9=/usr/local/Ascend/cann-9.0.0
C9S="$RUN/cann9"
mkdir -p "$C9S/runtime"
for entry in "$C9"/*; do name=$(basename "$entry"); test "$name" = runtime || ln -s "$entry" "$C9S/$name"; done
for entry in "$C9/runtime"/*; do name=$(basename "$entry"); test "$name" = version.info || ln -s "$entry" "$C9S/runtime/$name"; done
cp /usr/local/Ascend/ascend-toolkit/latest/runtime/version.info "$C9S/runtime/version.info"
echo SETUP_OK
    """.strip()
    output = transport._remote("bash -lc " + shlex.quote(setup), cfg, timeout=300)
    transport._require_ack(output, "SETUP_OK", "setup")


def _common_env(remote_run: str) -> str:
    return (
        f"export ASCEND_HOME_PATH={shlex.quote(remote_run + '/cann9')} "
        "TORCH_DEVICE_BACKEND_AUTOLOAD=0 PIP_FORCE_REINSTALL=1 CANN_BUILD_JOBS=12; "
        "export LD_LIBRARY_PATH=/usr/local/Ascend/cann-9.0.0/lib64:${LD_LIBRARY_PATH:-}; "
    )


def _event_command(
    args: argparse.Namespace,
    *,
    remote_run: str,
    arm: str,
    source_name: str,
    sequence: int,
) -> str:
    metadata_env = " ".join(
        "PROFILER_AB_%s=%s" % (key.upper(), shlex.quote(str(value)))
        for key, value in {
            "operator": args.operator,
            "arm": arm,
            "case_id": args.case_id,
            "run_id": args.run_id,
            "soc": args.soc,
            "cann_version": args.cann_version,
            "compiler_version": args.compiler_version,
            "dtype": args.dtype,
            "shape_domain": args.shape_domain,
            "device": args.device,
            "sequence": sequence,
        }.items()
    )
    event_dir = f"{remote_run}/event_{arm}"
    return (
        _common_env(remote_run)
        + f"export PATH={shlex.quote(remote_run + '/venv/bin')}:$PATH; "
        + f"export {metadata_env} "
        + f"PROFILER_AB_INNER_REPEAT={args.inner_repeat} "
        + "PROFILER_AB_CACHE_CLEAN_EACH_SAMPLE="
        + ("0; " if args.event_cache_mode == "steady" else "1; ")
        + f"cd {shlex.quote(remote_run + '/bench_overlay')}; "
        + "./scripts/run_evaluation.sh "
        + f"--source-dir {shlex.quote(remote_run + '/' + source_name)} "
        + f"--task-dir {shlex.quote(remote_run + '/task')} "
        + f"--operator {shlex.quote(args.operator)} "
        + f"--device npu --device-id {args.device} "
        + f"--reports-dir {shlex.quote(event_dir + '/reports')} "
        + f"--warmup {args.warmup} --repeat {args.repeat} --no-perf-batch-cases"
    )


def _pmu_command(
    args: argparse.Namespace,
    *,
    remote_run: str,
    task_rel: str,
    metric: str,
    arm: str,
    source_name: str,
) -> str:
    pmu_dir = f"{remote_run}/pmu_{arm}"
    return (
        _common_env(remote_run)
        + f"export PATH={shlex.quote(remote_run + '/venv/bin')}:$PATH; "
        + f"cd {shlex.quote(remote_run)}; "
        + f"python3 pipeprof.py {shlex.quote(args.operator)} {shlex.quote(remote_run + '/' + source_name)} "
        + f"--bench {shlex.quote(args.bench)} --dev {args.device} "
        + f"--metric {shlex.quote(metric)} --level Level1 "
        + f"--out {shlex.quote(pmu_dir + '/reports')} "
        + f"--json-out {shlex.quote(pmu_dir + '/pipeprof.json')} "
        + f"--arm {arm} --case-id {args.case_id} --task-rel {shlex.quote(task_rel)} "
        + f"--run-id {shlex.quote(args.run_id)} --soc {shlex.quote(args.soc)} "
        + f"--cann-version {shlex.quote(args.cann_version)} "
        + f"--compiler-version {shlex.quote(args.compiler_version)} "
        + f"--dtype {shlex.quote(args.dtype)} --shape-domain {shlex.quote(args.shape_domain)}"
    )


def _confirm_no_evaluation(cfg) -> bool:
    script = r"""
if ps -eo command | grep -E '[r]un_evaluation|[p]rofiler_off_harness_patch|[p]ipeprof.py' >/dev/null; then
  echo EVALUATION_BUSY
else
  echo EVALUATION_IDLE
fi
""".strip()
    output = transport._remote("bash -lc " + shlex.quote(script), cfg, timeout=120)
    return "EVALUATION_IDLE" in transport._text(output).splitlines()


def _collect_remote(cfg, *, remote_run: str) -> bytes:
    """Collect every surviving artifact without requiring all four arm dirs."""
    collect = rf"""
set -u
RUN={shlex.quote(remote_run)}
cd "$RUN" || exit 91
roots=()
for root in event_base event_mt pmu_base pmu_mt; do
  test -e "$root" && roots+=("$root")
done
if test ${{#roots[@]}} -eq 0; then
  tar -czf - --files-from /dev/null
  exit 0
fi
find "${{roots[@]}}" -type f \
  ! -path '*/prof_data/*' \
  \( -name 'event_samples.json' -o -name 'cann_*eval*.json' \
     -o -name 'pipeprof.json' -o -name 'kernel_details.csv' \
     -o -name 'evaluation.log' \) -print0 \
  | tar --null -czf - --files-from -
""".strip()
    return transport._remote(
        "bash -lc " + shlex.quote(collect), cfg, timeout=300, display=False
    )


def _collect_and_extract(
    cfg,
    *,
    remote_run: str,
    pair_dir: Path,
    archive_path: Path,
) -> None:
    payload = _collect_remote(cfg, remote_run=remote_run)
    transport._safe_extract(
        payload,
        pair_dir,
        archive_path=archive_path,
        allow_existing=True,
    )


def _fallback_feedback(
    pair_dir: Path,
    *,
    action_cell: str,
    error: str,
) -> dict[str, Any]:
    return {
        "schema_version": "ascend-profiler-feedback-query/v1",
        "status": "rejected",
        "query": {
            "action_cell": action_cell,
            "base_arm": "base",
            "timing_stat": "p50",
            "mode": "single local query after profiler completion",
        },
        "artifacts": {"roots": [str(pair_dir.resolve())], "refs": []},
        "run_contexts": [],
        "correctness": {
            "cases": 0,
            "passed": 0,
            "failed": 0,
            "unknown": 0,
            "all_passed": False,
            "complete": False,
            "arms": [],
        },
        "timing": {"authority": "profiler-off NPU Event", "analyses": []},
        "pmu": {
            "role": "mechanism attribution only; profiler Duration is excluded as reward",
            "comparisons": [],
        },
        "compact_feedback": [],
        "adoption_evidence": [],
        "rejections": [
            {
                "code": "reducer_exception",
                "message": error,
                "artifact_refs": [],
            }
        ],
        "limitations": [
            {
                "code": "feedback_reduction_failed",
                "message": "the local reducer raised; inspect collected logs",
            }
        ],
        "notices": [],
    }


def _reduce_feedback(pair_dir: Path, *, action_cell: str) -> StageResult:
    output = pair_dir / "unified_feedback.json"
    temporary = pair_dir / ".unified_feedback.json.tmp"
    if output.exists() or temporary.exists():
        return StageResult(ok=False, error=f"refusing existing unified feedback output: {output}")
    try:
        payload = query_completed_run([pair_dir], action_cell=action_cell)
        text = serialize_feedback(payload)
        error = None
        ok = True
    except Exception as exc:
        error = _error_text(exc)
        payload = _fallback_feedback(pair_dir, action_cell=action_cell, error=error)
        text = serialize_feedback(payload)
        ok = False
    temporary.write_text(text, encoding="utf-8")
    _os_replace(temporary, output)
    return StageResult(ok=ok, value=(output, payload), error=error)


def _project_feedback(
    pair_dir: Path,
    feedback: Mapping[str, Any],
    *,
    token_budget: int,
) -> StageResult:
    output = pair_dir / "agent_feedback.json"
    temporary = pair_dir / ".agent_feedback.json.tmp"
    if output.exists() or temporary.exists():
        return StageResult(ok=False, error=f"refusing existing agent feedback output: {output}")
    try:
        projection = build_agent_projection(feedback, token_budget=token_budget)
        extraction = projection.get("extraction") or {}
        tokens = int(extraction.get("tokens") or 0)
        if extraction.get("budget_satisfied") is False or tokens > token_budget:
            raise ValueError(
                f"agent projection exceeded token budget: {tokens}/{token_budget}"
            )
        temporary.write_text(serialize_projection(projection), encoding="utf-8")
        _os_replace(temporary, output)
        return StageResult(ok=True, value=(output, projection))
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return StageResult(ok=False, error=_error_text(exc))


def _extract_reward(feedback: Mapping[str, Any], projection: Mapping[str, Any] | None) -> dict[str, Any]:
    if projection:
        rewards = projection.get("rewards") or []
        if rewards:
            reward = rewards[0]
            return {
                "speedup": reward.get("speedup"),
                "lcb95": reward.get("lcb95"),
                "accepted": reward.get("accepted"),
                "stable": reward.get("stable"),
                "verdict": (projection.get("decision") or {}).get("recommendation"),
            }
    for analysis in (feedback.get("timing") or {}).get("analyses") or []:
        for arm_result in (analysis.get("diff") or {}).values():
            rows = (arm_result or {}).get("rows") or []
            if rows:
                row = rows[0]
                return {
                    "speedup": row.get("x"),
                    "lcb95": row.get("speedup_95pct_lcb"),
                    "accepted": row.get("accepted"),
                    "stable": row.get("stable"),
                    "verdict": "accepted" if row.get("accepted") is True else "remeasure",
                }
    return {
        "speedup": None,
        "lcb95": None,
        "accepted": False,
        "stable": False,
        "verdict": "no_usable_event_evidence",
    }


def _attach_attempt_artifacts(
    attempts: list[dict[str, Any]],
    *,
    staging: Path,
    pair_rel: str,
    agent_entrypoint: str,
) -> None:
    refs_by_stage_arm: dict[tuple[str, str | None], list[str]] = {
        ("EVENT", "base"): [
            f"{pair_rel}/event_base/evaluation.log",
            f"{pair_rel}/event_base/event_samples.json",
        ],
        ("EVENT", "mt"): [
            f"{pair_rel}/event_mt/evaluation.log",
            f"{pair_rel}/event_mt/event_samples.json",
        ],
        ("PMU", "base"): [
            f"{pair_rel}/pmu_base/evaluation.log",
            f"{pair_rel}/pmu_base/pipeprof.json",
        ],
        ("PMU", "mt"): [
            f"{pair_rel}/pmu_mt/evaluation.log",
            f"{pair_rel}/pmu_mt/pipeprof.json",
        ],
        ("COLLECT", None): ["remote_artifacts.tgz"],
        ("REDUCE", None): [f"{pair_rel}/unified_feedback.json"],
        ("PROJECT", None): [f"{pair_rel}/{agent_entrypoint}"],
        ("COMMIT", None): [
            "run_manifest.json",
            "attempts.jsonl",
            "tournament_summary.json",
        ],
    }
    for attempt in attempts:
        refs = refs_by_stage_arm.get((str(attempt.get("stage")), attempt.get("arm")))
        if refs:
            attempt["artifact_refs"] = [
                ref for ref in refs if (staging / ref).is_file()
            ]


def _write_attempts(path: Path, attempts: list[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in attempts
    )
    _atomic_write_text(path, text)


def _publish_aliases(
    staging: Path,
    *,
    pair_dir: Path,
    agent_entrypoint: str,
) -> None:
    for name in ("run_metadata.json", "unified_feedback.json"):
        source = pair_dir / name
        if source.is_file():
            _atomic_copy(source, staging / name)
    if agent_entrypoint == "agent_feedback.json":
        source = pair_dir / "agent_feedback.json"
        if source.is_file():
            _atomic_copy(source, staging / "agent_feedback.json")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--school-env", type=Path, required=True)
    parser.add_argument("--base-source", type=Path, required=True)
    parser.add_argument("--candidate-source", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--operator", default="Maximum")
    parser.add_argument("--level", default="level2")
    parser.add_argument("--snake", default="")
    parser.add_argument("--case-id", type=int, default=11)
    parser.add_argument("--bench", default="/home/demo2/cann-bench-20260722")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=30)
    parser.add_argument("--inner-repeat", type=int, default=1)
    parser.add_argument(
        "--event-cache-mode", choices=("cannbench", "steady"), default="cannbench"
    )
    parser.add_argument("--metric", action="append", default=None)
    parser.add_argument("--soc", default="Ascend910C")
    parser.add_argument("--cann-version", default="9.0.0")
    parser.add_argument("--compiler-version", default="bisheng-15.0.5")
    parser.add_argument("--dtype", default="fp32")
    parser.add_argument("--shape-domain", default="shared_row")
    parser.add_argument("--action-cell", default="async_pipeline.shared_row_prefetch")
    parser.add_argument("--phase-timeout", type=int, default=1800)
    parser.add_argument("--agent-token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Path, Path, str, str]:
    if args.run_id is None:
        args.run_id = time.strftime("%Y%m%dT%H%M%S") + f"_{os.getpid()}"
    for path in (args.school_env, args.base_source, args.candidate_source):
        if not path.exists():
            parser.error(f"missing path: {path}")
    if not args.school_env.is_file():
        parser.error(f"school environment is not a file: {args.school_env}")
    if not args.base_source.is_dir() or not args.candidate_source.is_dir():
        parser.error("base and candidate sources must be directories")
    for source in (args.base_source, args.candidate_source):
        if not (source / "CMakeLists.txt").is_file():
            parser.error(f"source lacks CMakeLists.txt: {source}")
    if args.artifact_dir.exists():
        parser.error(f"artifact directory already exists: {args.artifact_dir}")
    if args.device < 0:
        parser.error("--device must be non-negative")
    if args.case_id < 0:
        parser.error("--case-id must be non-negative")
    if args.warmup < 0:
        parser.error("--warmup must be non-negative")
    if args.repeat < 30:
        parser.error("profiler-off p50 pilot requires at least 30 Event samples")
    if not 1 <= args.inner_repeat <= 1000:
        parser.error("--inner-repeat must be in [1, 1000]")
    if args.phase_timeout < 120:
        parser.error("--phase-timeout must be at least 120 seconds")
    if args.agent_token_budget < 128:
        parser.error("--agent-token-budget must be at least 128")
    try:
        transport._remote_run_path(args.run_id)
        level = _validate_component(args.level, "--level")
        base = args.base_source.resolve()
        candidate = args.candidate_source.resolve()
        snake = _validate_component((args.snake or base.name).strip(), "--snake")
    except ValueError as exc:
        parser.error(str(exc))
    return base, candidate, level, snake


def _main_impl(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    base_source, candidate_source, level, snake = _validate_args(parser, args)
    args.level = level
    metric = (args.metric or ["PipeUtilization"])[-1]
    task_rel = f"{level}/{snake}"
    remote_run = transport._remote_run_path(args.run_id)
    archive_path = remote_run + ".tgz"

    # Freeze source identity before touching the device.  Packaging remains a
    # separately timed stage and can fail without losing the experiment key.
    source_hashes = {
        "base": transport._source_tree_sha256(base_source),
        "mt": transport._source_tree_sha256(candidate_source),
    }
    experiment_id, experiment_payload = _experiment_identity(
        args, source_hashes=source_hashes, snake=snake, metric=metric
    )
    print(
        "SOURCE_SHA256 base=%s mt=%s op=%s task=%s experiment_id=%s"
        % (
            source_hashes["base"],
            source_hashes["mt"],
            args.operator,
            task_rel,
            experiment_id,
        ),
        flush=True,
    )
    context = RunContext(
        run_id=args.run_id,
        experiment_id=experiment_id,
        case_id=args.case_id,
        device=args.device,
        attempts=[],
    )

    started_at = _utc_now()
    args.artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = args.artifact_dir.parent / f".{args.artifact_dir.name}.profiler-run-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    pair_rel = f"pairs/c{args.case_id}__mt"
    pair_dir = staging / pair_rel
    pair_dir.mkdir(parents=True, exist_ok=False)

    cfg: object | None = None
    precheck_text = ""
    payload: bytes | None = None
    remote_ready = False
    base_failed = False
    candidate_failed = False
    partial = False
    collection_ok = False
    feedback: Mapping[str, Any] = _fallback_feedback(
        pair_dir, action_cell=args.action_cell, error="run did not reach reduction"
    )
    projection: Mapping[str, Any] | None = None

    precheck = _run_stage(context, "PRECHECK", lambda: _precheck(args))
    if precheck.ok:
        cfg, precheck_text = precheck.value
        precheck_text = str(precheck_text)
        package = _run_stage(
            context,
            "PACKAGE",
            lambda: transport._package(base_source, candidate_source, snake=snake),
        )
        if package.ok:
            payload = bytes(package.value)
            print(f"UPLOAD bytes={len(payload)}", flush=True)
            upload = _run_stage(
                context,
                "UPLOAD",
                lambda: _upload(cfg, archive_path=archive_path, payload=payload),
            )
            if upload.ok:
                setup = _run_stage(
                    context,
                    "SETUP",
                    lambda: _setup_remote(
                        args,
                        cfg,
                        remote_run=remote_run,
                        archive_path=archive_path,
                        task_rel=task_rel,
                    ),
                )
                remote_ready = setup.ok
            else:
                _skip_stage(context, "SETUP", "upload_failed")
        else:
            _skip_stage(context, "UPLOAD", "package_failed")
            _skip_stage(context, "SETUP", "package_failed")
    else:
        _skip_stage(context, "PACKAGE", "precheck_failed")
        _skip_stage(context, "UPLOAD", "precheck_failed")
        _skip_stage(context, "SETUP", "precheck_failed")

    if remote_ready:
        assert cfg is not None
        event_base = _run_stage(
            context,
            "EVENT",
            lambda: StageResult(
                ok=True,
                remote_elapsed_s=transport._phase(
                    _event_command(
                        args,
                        remote_run=remote_run,
                        arm="base",
                        source_name="base_source",
                        sequence=1,
                    ),
                    f"{remote_run}/event_base/evaluation.log",
                    "event_base",
                    cfg,
                    timeout=args.phase_timeout,
                ),
            ),
            arm="base",
        )
        if not event_base.ok:
            base_failed = True
            _skip_stage(context, "EVENT", "base_failed", arm="mt")
            idle = _run_stage(
                context,
                "CONFIRM_IDLE",
                lambda: StageResult(
                    ok=_confirm_no_evaluation(cfg),
                    error="remote evaluator is still active",
                ),
                arm="base",
            )
            partial = partial or not idle.ok
        else:
            event_mt = _run_stage(
                context,
                "EVENT",
                lambda: StageResult(
                    ok=True,
                    remote_elapsed_s=transport._phase(
                        _event_command(
                            args,
                            remote_run=remote_run,
                            arm="mt",
                            source_name="mt_source",
                            sequence=2,
                        ),
                        f"{remote_run}/event_mt/evaluation.log",
                        "event_mt",
                        cfg,
                        timeout=args.phase_timeout,
                    ),
                ),
                arm="mt",
            )
            if not event_mt.ok:
                candidate_failed = True
                partial = True
                idle = _run_stage(
                    context,
                    "CONFIRM_IDLE",
                    lambda: StageResult(
                        ok=_confirm_no_evaluation(cfg),
                        error="remote evaluator is still active",
                    ),
                    arm="mt",
                )
                if not idle.ok:
                    partial = True

        if base_failed or candidate_failed:
            _skip_stage(context, "PMU", "event_evidence_incomplete", arm="base")
            _skip_stage(context, "PMU", "event_evidence_incomplete", arm="mt")
        else:
            pmu_base = _run_stage(
                context,
                "PMU",
                lambda: StageResult(
                    ok=True,
                    remote_elapsed_s=transport._phase(
                        _pmu_command(
                            args,
                            remote_run=remote_run,
                            task_rel=task_rel,
                            metric=metric,
                            arm="base",
                            source_name="base_source",
                        ),
                        f"{remote_run}/pmu_base/evaluation.log",
                        "pmu_base",
                        cfg,
                        timeout=args.phase_timeout,
                    ),
                ),
                arm="base",
            )
            if not pmu_base.ok:
                idle = _run_stage(
                    context,
                    "CONFIRM_IDLE",
                    lambda: StageResult(
                        ok=_confirm_no_evaluation(cfg),
                        error="remote profiler is still active",
                    ),
                    arm="base",
                )
            else:
                idle = StageResult(ok=True)
            if idle.ok:
                pmu_mt = _run_stage(
                    context,
                    "PMU",
                    lambda: StageResult(
                        ok=True,
                        remote_elapsed_s=transport._phase(
                            _pmu_command(
                                args,
                                remote_run=remote_run,
                                task_rel=task_rel,
                                metric=metric,
                                arm="mt",
                                source_name="mt_source",
                            ),
                            f"{remote_run}/pmu_mt/evaluation.log",
                            "pmu_mt",
                            cfg,
                            timeout=args.phase_timeout,
                        ),
                    ),
                    arm="mt",
                )
            else:
                _skip_stage(context, "PMU", "previous_profiler_still_active", arm="mt")
                pmu_mt = StageResult(ok=False, error="previous profiler is still active")
            partial = partial or not pmu_base.ok or not pmu_mt.ok
    else:
        _skip_stage(context, "EVENT", "remote_setup_incomplete", arm="base")
        _skip_stage(context, "EVENT", "remote_setup_incomplete", arm="mt")
        _skip_stage(context, "PMU", "remote_setup_incomplete", arm="base")
        _skip_stage(context, "PMU", "remote_setup_incomplete", arm="mt")

    if precheck.ok:
        assert cfg is not None
        collect = _run_stage(
            context,
            "COLLECT",
            lambda: _collect_and_extract(
                cfg,
                remote_run=remote_run,
                pair_dir=pair_dir,
                archive_path=staging / "remote_artifacts.tgz",
            ),
        )
        collection_ok = collect.ok
        partial = partial or not collect.ok
    else:
        _skip_stage(context, "COLLECT", "precheck_failed")

    # Release the remote lease immediately after best-effort collection.  The
    # remaining work is pure local reduction/projection/commit.
    transport._release_active_remote_lock()

    pair_metadata: dict[str, Any] = {
        "schema_version": PAIR_SCHEMA_VERSION,
        "run_id": args.run_id,
        "experiment_id": experiment_id,
        "remote_run": remote_run,
        "device": args.device,
        "operator": args.operator,
        "snake": snake,
        "task_rel": task_rel,
        "case_id": args.case_id,
        "metric": metric,
        "action_cell": args.action_cell,
        "event_timing": {
            "outer_samples": args.repeat,
            "inner_repeat": args.inner_repeat,
            "cache_mode": args.event_cache_mode,
        },
        "source_tree_sha256": source_hashes,
        "sequence": ["event_base", "event_mt", "pmu_base", "pmu_mt"],
        "agent_feedback_entrypoint": "agent_feedback.json",
        "audit_feedback_entrypoint": "unified_feedback.json",
        "precheck": precheck_text,
        "execution": {
            "backend": BACKEND_NAME,
            "collection_ok": collection_ok,
            "stage_status": [
                {
                    "stage": row["stage"],
                    "arm": row.get("arm"),
                    "status": row["status"],
                    "error": row.get("error"),
                }
                for row in context.attempts
            ],
        },
    }
    _atomic_write_json(pair_dir / "run_metadata.json", pair_metadata)

    reduced = _run_stage(
        context,
        "REDUCE",
        lambda: _reduce_feedback(pair_dir, action_cell=args.action_cell),
    )
    if reduced.value:
        _, feedback = reduced.value
    partial = partial or not reduced.ok or str(feedback.get("status")) == "partial"

    projected = _run_stage(
        context,
        "PROJECT",
        lambda: _project_feedback(
            pair_dir,
            feedback,
            token_budget=args.agent_token_budget,
        ),
    )
    agent_entrypoint = "agent_feedback.json" if projected.ok else "unified_feedback.json"
    if projected.value:
        _, projection = projected.value
    if not projected.ok:
        pair_metadata["agent_feedback_entrypoint"] = "unified_feedback.json"
        pair_metadata.setdefault("execution", {})["projection_caveat"] = projected.error
        _atomic_write_json(pair_dir / "run_metadata.json", pair_metadata)

    feedback_status = str(feedback.get("status") or "rejected")
    reward = _extract_reward(feedback, projection)
    if base_failed:
        exit_code = 2
    elif candidate_failed:
        exit_code = 3
    elif feedback_status == "rejected":
        exit_code = 4
    elif partial or feedback_status == "partial":
        exit_code = 3
    else:
        exit_code = 0
    run_status = {0: "succeeded", 2: "failed", 3: "partial", 4: "failed"}[exit_code]

    finished_at = _utc_now()
    pair_status = "ok" if exit_code == 0 else ("partial" if exit_code == 3 else "failed")
    tournament_summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "run_id": args.run_id,
        "experiment_id": experiment_id,
        "status": run_status,
        "base_arm": "base",
        "comparisons": [
            {
                "case_id": args.case_id,
                "arm": "mt",
                "status": pair_status,
                **reward,
                "dir": pair_rel,
            }
        ],
    }

    run_manifest: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": args.run_id,
        "experiment_id": experiment_id,
        "experiment_key": experiment_payload,
        "replicate_id": 1,
        "rerun_reason": None,
        "operator": args.operator,
        "action_cell": args.action_cell,
        "backend": BACKEND_NAME,
        "device": args.device,
        "case_ids": [args.case_id],
        "arms": [
            {
                "name": "base",
                "source_tree_sha256": source_hashes["base"],
                "role": "base",
            },
            {
                "name": "mt",
                "source_tree_sha256": source_hashes["mt"],
                "role": "candidate",
            },
        ],
        "stage_durations": [
            {
                "stage": row["stage"],
                "arm": row.get("arm"),
                "case_id": row.get("case_id"),
                "elapsed_s": row["elapsed_s"],
                "remote_elapsed_s": row.get("remote_elapsed_s"),
                "status": row["status"],
                "error": row.get("error"),
            }
            for row in context.attempts
        ],
        "pmu_selection": {"mode": "all", "winners": ["mt"] if reward.get("accepted") else []},
        "pairs": [
            {
                "case_id": args.case_id,
                "arm": "mt",
                "status": pair_status,
                "speedup": reward.get("speedup"),
                "lcb95": reward.get("lcb95"),
                "verdict": reward.get("verdict"),
                "dir": pair_rel,
            }
        ],
        "entrypoints": {
            "audit": "unified_feedback.json",
            "agent": agent_entrypoint,
        },
        "caveats": (
            [{"code": "agent_projection_failed", "message": projected.error}]
            if not projected.ok
            else []
        ),
        "status": run_status,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
    }

    _publish_aliases(staging, pair_dir=pair_dir, agent_entrypoint=agent_entrypoint)

    commit_started_at = _utc_now()
    commit_started = time.monotonic()
    # The COMMIT row is part of the same staged directory.  The final os.replace
    # is the only visibility point for the complete run.
    commit_row = _record_attempt(
        context,
        stage="COMMIT",
        status="succeeded",
        started_at=commit_started_at,
        elapsed_s=0.0,
    )
    run_manifest["stage_durations"].append(
        {
            "stage": "COMMIT",
            "arm": None,
            "case_id": args.case_id,
            "elapsed_s": 0.0,
            "remote_elapsed_s": None,
            "status": "succeeded",
            "error": None,
        }
    )
    _atomic_write_json(staging / "tournament_summary.json", tournament_summary)
    _atomic_write_json(staging / "run_manifest.json", run_manifest)
    _write_attempts(staging / "attempts.jsonl", context.attempts)
    _attach_attempt_artifacts(
        context.attempts,
        staging=staging,
        pair_rel=pair_rel,
        agent_entrypoint=agent_entrypoint,
    )
    commit_elapsed = time.monotonic() - commit_started
    commit_row["elapsed_s"] = round(commit_elapsed, 6)
    run_manifest["stage_durations"][-1]["elapsed_s"] = round(commit_elapsed, 6)
    _atomic_write_json(staging / "run_manifest.json", run_manifest)
    _write_attempts(staging / "attempts.jsonl", context.attempts)
    args.artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    _os_replace(staging, args.artifact_dir)

    print(f"ARTIFACTS {args.artifact_dir}", flush=True)
    print(
        f"UNIFIED_FEEDBACK status={feedback_status} path={args.artifact_dir / 'unified_feedback.json'}",
        flush=True,
    )
    print(
        f"PROFILE_COMPLETION status={run_status} exit_code={exit_code} "
        f"manifest={args.artifact_dir / 'run_manifest.json'}",
        flush=True,
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    try:
        return _main_impl(argv)
    finally:
        transport._release_active_remote_lock()


if __name__ == "__main__":
    raise SystemExit(main())
