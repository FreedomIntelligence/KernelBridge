#!/usr/bin/env python3
"""Run one same-card base plus many candidate NPU-Event timing trials.

The sources are packaged once and evaluated strictly serially under the same
remote device lock.  This tool deliberately does not collect PMU data.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import statistics
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import real_profiler_ab as ab

# Windows PowerShell commonly inherits a GBK text stream.  Remote evaluator
# logs contain Unicode status glyphs, so make display robust without changing
# the byte-oriented SSH/upload/collect paths.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")


@dataclass(frozen=True)
class Candidate:
    name: str
    safe_name: str
    source: Path

    @property
    def stage_name(self) -> str:
        return f"candidate_{self.safe_name}"

    @property
    def event_name(self) -> str:
        return f"event_{self.safe_name}"


def _safe_candidate_name(name: str) -> str:
    """Make one deterministic, short POSIX/Windows-safe path component."""
    original = name.strip()
    if not original:
        raise ValueError("candidate NAME must not be empty")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", original).strip("._-")
    if not slug:
        slug = "candidate"
    if not slug[0].isalnum():
        slug = "c_" + slug
    changed = slug != original
    if slug.casefold() == "base" or len(slug) > 32 or changed:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug[:23]}_{digest}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", slug):
        raise ValueError(f"could not derive a safe candidate name from {name!r}")
    return slug


def _parse_candidates(values: list[str], parser: argparse.ArgumentParser) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            parser.error(f"--candidate expects NAME=PATH, got {value!r}")
        name, raw_path = value.split("=", 1)
        name = name.strip()
        if not raw_path.strip():
            parser.error(f"candidate {name!r} has an empty PATH")
        try:
            safe_name = _safe_candidate_name(name)
        except ValueError as exc:
            parser.error(str(exc))
        collision = seen.get(safe_name.casefold())
        if collision is not None:
            parser.error(
                f"candidate names {collision!r} and {name!r} map to the same safe name "
                f"{safe_name!r}"
            )
        source = Path(raw_path).expanduser()
        if not source.is_dir():
            parser.error(f"candidate source is not a directory: {source}")
        if not (source / "CMakeLists.txt").is_file():
            parser.error(f"candidate source lacks CMakeLists.txt: {source}")
        seen[safe_name.casefold()] = name
        candidates.append(Candidate(name=name, safe_name=safe_name, source=source.resolve()))
    return candidates


def _validate_component(value: str, option: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError(f"{option} must be one safe [A-Za-z0-9_.-] path component")
    if value in {".", ".."}:
        raise ValueError(f"unsafe {option}: {value!r}")
    return value


def _package(base_source: Path, candidates: list[Candidate], *, snake: str) -> bytes:
    template = ab.REPO / "native" / "cann_submit" / "template"
    required = {
        "template ops dispatcher": template / "csrc" / "ops" / "CMakeLists.txt",
        "base operator registration": base_source / "CMakeLists.txt",
    }
    required.update(
        {
            f"candidate {candidate.name!r} operator registration": (
                candidate.source / "CMakeLists.txt"
            )
            for candidate in candidates
        }
    )
    missing = [f"{label}: {path}" for label, path in required.items() if not path.is_file()]
    if missing:
        raise ValueError("invalid multi-Event package input; missing " + "; ".join(missing))

    # Windows indexing/AV can transiently hold template metadata (observed on the
    # copied .gitignore) after the tar stream is already complete.  The package is
    # fully memory-backed, so cleanup contention must not discard a valid payload.
    with tempfile.TemporaryDirectory(
        prefix="profiler_multi_event_stage_", ignore_cleanup_errors=True
    ) as temporary:
        stage = Path(temporary)
        ab._copy_source(template, base_source, stage / "base_source", snake=snake)
        for candidate in candidates:
            ab._copy_source(
                template,
                candidate.source,
                stage / candidate.stage_name,
                snake=snake,
            )
        shutil.copy2(
            ab.TOOLS / "profiler_off_harness_patch.py",
            stage / "profiler_off_harness_patch.py",
        )
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for child in sorted(stage.iterdir(), key=lambda item: item.name):
                archive.add(child, arcname=child.name)
        return stream.getvalue()


def _setup_remote(
    cfg,
    *,
    remote_run: str,
    archive_path: str,
    bench: str,
    task_rel: str,
    case_id: int,
) -> None:
    setup = f"""
set -euo pipefail
RUN={shlex.quote(remote_run)}
BENCH={shlex.quote(bench)}
test "$RUN" != /tmp
rm -rf "$RUN"
mkdir -p "$RUN"
tar xzf {shlex.quote(archive_path)} -C "$RUN"
mkdir -p "$RUN/bench_overlay"
cp -a "$BENCH/src" "$RUN/bench_overlay/src"
cp -a "$BENCH/scripts" "$RUN/bench_overlay/scripts"
cp "$BENCH/VERSION" "$RUN/bench_overlay/VERSION" 2>/dev/null || true
cp "$BENCH/pyproject.toml" "$RUN/bench_overlay/pyproject.toml" 2>/dev/null || true
mkdir -p "$RUN/task/{task_rel.rsplit('/', 1)[0]}"
cp -a "$BENCH/tasks/{task_rel}" "$RUN/task/{task_rel}"
ln -s "$RUN/task" "$RUN/bench_overlay/tasks"
chmod -R u+w "$RUN"
python3 - "$RUN/task/{task_rel}/cases.yaml" {case_id} <<'PY'
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
PY
python3 "$RUN/profiler_off_harness_patch.py" "$RUN/bench_overlay/src/kernel_eval/eval/perf_eval.py"
python3 - "$RUN/bench_overlay/src/cann_bench_utils/setup.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
old = "cpu_count = os.cpu_count() or 2"
new = 'cpu_count = int(os.environ.get("CANN_BUILD_JOBS") or (os.cpu_count() or 2))'
assert text.count(old) == 1, text.count(old)
path.write_text(text.replace(old, new))
PY
python3 -m venv --system-site-packages "$RUN/venv"
C9=/usr/local/Ascend/cann-9.0.0
C9S="$RUN/cann9"
mkdir -p "$C9S/runtime"
for entry in "$C9"/*; do name=$(basename "$entry"); test "$name" = runtime || ln -s "$entry" "$C9S/$name"; done
for entry in "$C9/runtime"/*; do name=$(basename "$entry"); test "$name" = version.info || ln -s "$entry" "$C9S/runtime/$name"; done
cp /usr/local/Ascend/ascend-toolkit/latest/runtime/version.info "$C9S/runtime/version.info"
echo SETUP_OK
    """.strip()
    print("SETUP", flush=True)
    output = ab._remote("bash -lc " + shlex.quote(setup), cfg, timeout=300)
    ab._require_ack(output, "SETUP_OK", "setup")


def _event_command(
    *,
    remote_run: str,
    source_name: str,
    event_name: str,
    arm: str,
    sequence: int,
    args: argparse.Namespace,
    case_id: int,
) -> str:
    common_env = (
        f"export ASCEND_HOME_PATH={shlex.quote(remote_run + '/cann9')} "
        "TORCH_DEVICE_BACKEND_AUTOLOAD=0 PIP_FORCE_REINSTALL=1 CANN_BUILD_JOBS=12; "
        "export LD_LIBRARY_PATH=/usr/local/Ascend/cann-9.0.0/lib64:${LD_LIBRARY_PATH:-}; "
    )
    metadata_env = " ".join(
        f"PROFILER_AB_{key.upper()}={shlex.quote(str(value))}"
        for key, value in {
            "operator": args.operator,
            "arm": arm,
            "case_id": case_id,
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
    event_dir = f"{remote_run}/{event_name}"
    return (
        common_env
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


def _confirm_no_evaluation(cfg) -> bool:
    """Only continue after a failed phase when the remote evaluator is gone."""
    script = r"""
if ps -eo command | grep -E '[r]un_evaluation|[p]rofiler_off_harness_patch' >/dev/null; then
  echo EVALUATION_BUSY
else
  echo EVALUATION_IDLE
fi
""".strip()
    output = ab._remote("bash -lc " + shlex.quote(script), cfg, timeout=120)
    return "EVALUATION_IDLE" in ab._text(output).splitlines()


def _collect_flat(
    cfg,
    *,
    remote_run: str,
    event_names: list[str],
    artifact_dir: Path,
) -> None:
    # All event names have already passed the component validator.  JSON is
    # still used to avoid constructing executable shell fragments from them.
    names_json = json.dumps(event_names, ensure_ascii=True)
    collect = f"""
set -euo pipefail
RUN={shlex.quote(remote_run)}
rm -rf "$RUN/collect_flat"
mkdir -p "$RUN/collect_flat"
python3 - "$RUN" <<'PY'
import json
import shutil
import sys
from pathlib import Path

run = Path(sys.argv[1])
event_names = json.loads({names_json!r})
root = run / "collect_flat"
for event_name in event_names:
    source = run / event_name
    target = root / event_name
    target.mkdir(parents=True, exist_ok=True)
    log = source / "evaluation.log"
    if log.is_file():
        shutil.copy2(log, target / "evaluation.log")
    events = sorted(source.rglob("event_samples.json"), key=lambda p: (p.stat().st_mtime_ns, str(p)))
    if events:
        shutil.copy2(events[-1], target / "event_samples.json")
    finals = sorted(source.rglob("cann_*eval*.json"), key=lambda p: (p.stat().st_mtime_ns, str(p)))
    if finals:
        shutil.copy2(finals[-1], target / "final.json")
PY
cd "$RUN/collect_flat"
tar -czf - {" ".join(shlex.quote(name) for name in event_names)}
    """.strip()
    payload = ab._remote(
        "bash -lc " + shlex.quote(collect), cfg, timeout=300, display=False
    )
    ab._safe_extract(payload, artifact_dir)


def _event_summary(path: Path) -> dict[str, object]:
    report = json.loads(path.read_text(encoding="utf-8"))
    raw_samples = report.get("samples_us")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ValueError("event report has no samples_us")
    samples = [float(value) for value in raw_samples]
    if any(not math.isfinite(value) or value <= 0 for value in samples):
        raise ValueError("event report contains a non-finite or non-positive sample")
    median = statistics.median(samples)
    deviations = [abs(value - median) for value in samples]
    return {
        "samples": len(samples),
        "p50_us": median,
        "min_us": min(samples),
        "max_us": max(samples),
        "mad_us": statistics.median(deviations),
    }


def _write_local_results(
    artifact_dir: Path,
    *,
    metadata: dict[str, object],
    sequence: list[dict[str, object]],
) -> dict[str, object]:
    summaries: dict[str, dict[str, object]] = {}
    errors: dict[str, str] = {}
    for entry in sequence:
        event_name = str(entry["event_name"])
        report_path = artifact_dir / event_name / "event_samples.json"
        if not report_path.is_file():
            errors[str(entry["arm"])] = "event_samples.json was not collected"
            continue
        try:
            summaries[str(entry["arm"])] = _event_summary(report_path)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            errors[str(entry["arm"])] = f"{type(exc).__name__}: {exc}"

    base = summaries.get("base")
    comparisons = []
    for entry in sequence[1:]:
        arm = str(entry["arm"])
        candidate = summaries.get(arm)
        if base is None or candidate is None:
            continue
        base_p50 = float(base["p50_us"])
        candidate_p50 = float(candidate["p50_us"])
        comparisons.append(
            {
                "arm": arm,
                "candidate_name": entry.get("candidate_name"),
                "base_p50_us": base_p50,
                "candidate_p50_us": candidate_p50,
                "delta_us": candidate_p50 - base_p50,
                "speedup": base_p50 / candidate_p50,
                "improved": candidate_p50 < base_p50,
            }
        )
    summary = {
        "schema_version": "real-profiler-multi-event-summary/v1",
        "run_id": metadata["run_id"],
        "arms": summaries,
        "comparisons": comparisons,
        "artifact_errors": errors,
    }
    (artifact_dir / "multi_event_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _main_impl(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--school-env", type=Path, required=True)
    parser.add_argument("--base-source", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="candidate label and source tree; repeat for multiple policies",
    )
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--operator", default="Maximum")
    parser.add_argument("--level", default="level2")
    parser.add_argument("--snake", default="")
    parser.add_argument("--case-id", action="append", type=int, default=None)
    parser.add_argument("--bench", required=True, help="CANN benchmark root on the selected host")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=15)
    parser.add_argument("--inner-repeat", type=int, default=1)
    parser.add_argument(
        "--event-cache-mode",
        "--cache-mode",
        dest="event_cache_mode",
        choices=("cannbench", "steady"),
        default="cannbench",
    )
    parser.add_argument("--phase-timeout", type=int, default=1800)
    parser.add_argument("--soc", default="Ascend910C")
    parser.add_argument("--cann-version", default="9.0.0")
    parser.add_argument("--compiler-version", default="bisheng-15.0.5")
    parser.add_argument("--dtype", default="fp32")
    parser.add_argument("--shape-domain", default="shared_row")
    args = parser.parse_args(argv)

    if args.run_id is None:
        args.run_id = time.strftime("%Y%m%dT%H%M%S") + f"_{os.getpid()}"
    case_ids = args.case_id or [11]
    if len(case_ids) != 1:
        parser.error("this version accepts repeated --case-id syntax but runs exactly one case")
    case_id = case_ids[0]
    if case_id < 0:
        parser.error("--case-id must be non-negative")
    if args.device < 0:
        parser.error("--device must be non-negative")
    if not args.school_env.is_file():
        parser.error(f"missing school environment file: {args.school_env}")
    if not args.base_source.is_dir():
        parser.error(f"base source is not a directory: {args.base_source}")
    if not (args.base_source / "CMakeLists.txt").is_file():
        parser.error(f"base source lacks CMakeLists.txt: {args.base_source}")
    if args.artifact_dir.exists():
        parser.error(f"artifact directory already exists: {args.artifact_dir}")
    if args.warmup < 0:
        parser.error("--warmup must be non-negative")
    if args.repeat < 5:
        parser.error("--repeat must be at least 5 Event samples")
    if not 1 <= args.inner_repeat <= 1000:
        parser.error("--inner-repeat must be in [1, 1000]")
    if args.phase_timeout < 120:
        parser.error("--phase-timeout must be at least 120 seconds")

    candidates = _parse_candidates(args.candidate, parser)
    resolved_base = args.base_source.resolve()
    try:
        level = _validate_component(args.level, "--level")
        snake = _validate_component(
            (args.snake or resolved_base.name).strip(), "--snake"
        )
        remote_run = ab._remote_run_path(args.run_id)
    except ValueError as exc:
        parser.error(str(exc))
    task_rel = f"{level}/{snake}"

    source_hashes = {
        "base": ab._source_tree_sha256(resolved_base),
        "candidates": {
            candidate.safe_name: ab._source_tree_sha256(candidate.source)
            for candidate in candidates
        },
    }
    payload = _package(resolved_base, candidates, snake=snake)
    candidate_hash_text = " ".join(
        f"{candidate.safe_name}={source_hashes['candidates'][candidate.safe_name]}"
        for candidate in candidates
    )
    print(
        f"SOURCE_SHA256 base={source_hashes['base']} {candidate_hash_text}",
        flush=True,
    )

    cfg = ab.load_config(args.school_env)
    print("PRECHECK", flush=True)
    precheck = ab._remote(
        "ps -eo pid,etime,command | grep -E '[r]un_evaluation|[p]ipeprof|[a]b20_' || true; "
        "npu-smi info",
        cfg,
        timeout=120,
    )
    precheck_text = ab._text(precheck)
    if "run_evaluation.sh" in precheck_text or "pipeprof.py" in precheck_text:
        raise RuntimeError("another evaluation is active; refusing to overlap")
    usages = ab._aicore_usages(precheck_text)
    if args.device not in usages:
        raise RuntimeError(f"could not read AICore utilization for device {args.device}")
    if usages[args.device] > 5:
        raise RuntimeError(
            f"device {args.device} is busy at {usages[args.device]}% AICore; refusing to overlap"
        )
    ab._acquire_remote_lock(cfg, device=args.device, owner=args.run_id)

    archive_path = remote_run + ".tgz"
    print(f"UPLOAD bytes={len(payload)}", flush=True)
    upload_output = ab._remote(
        "umask 077; cat > " + shlex.quote(archive_path) + "; echo UPLOAD_OK",
        cfg,
        timeout=180,
        stdin=payload,
    )
    ab._require_ack(upload_output, "UPLOAD_OK", "upload")
    _setup_remote(
        cfg,
        remote_run=remote_run,
        archive_path=archive_path,
        bench=args.bench,
        task_rel=task_rel,
        case_id=case_id,
    )

    sequence: list[dict[str, object]] = [
        {
            "sequence": 1,
            "role": "base",
            "arm": "base",
            "candidate_name": None,
            "stage_name": "base_source",
            "event_name": "event_base",
            "source_tree_sha256": source_hashes["base"],
        }
    ]
    sequence.extend(
        {
            "sequence": index,
            "role": "candidate",
            "arm": candidate.safe_name,
            "candidate_name": candidate.name,
            "stage_name": candidate.stage_name,
            "event_name": candidate.event_name,
            "source_tree_sha256": source_hashes["candidates"][candidate.safe_name],
        }
        for index, candidate in enumerate(candidates, start=2)
    )

    for entry in sequence:
        started = time.monotonic()
        event_name = str(entry["event_name"])
        label = f"{entry['sequence']}_{event_name}"
        command = _event_command(
            remote_run=remote_run,
            source_name=str(entry["stage_name"]),
            event_name=event_name,
            arm=str(entry["arm"]),
            sequence=int(entry["sequence"]),
            args=args,
            case_id=case_id,
        )
        print(f"EVENT sequence={entry['sequence']} arm={entry['arm']}", flush=True)
        try:
            ab._phase(
                command,
                f"{remote_run}/{event_name}/evaluation.log",
                label,
                cfg,
                timeout=args.phase_timeout,
            )
            entry["status"] = "ok"
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"[:4000]
            print(
                f"EVENT_FAILED arm={entry['arm']} error={entry['error']}",
                flush=True,
            )
            if not _confirm_no_evaluation(cfg):
                raise RuntimeError(
                    f"event arm {entry['arm']} failed while the evaluator is still active; "
                    "refusing to overlap the next arm"
                ) from exc
        finally:
            entry["elapsed_seconds"] = round(time.monotonic() - started, 3)

    print("COLLECT", flush=True)
    _collect_flat(
        cfg,
        remote_run=remote_run,
        event_names=[str(entry["event_name"]) for entry in sequence],
        artifact_dir=args.artifact_dir,
    )

    metadata: dict[str, object] = {
        "schema_version": "real-profiler-multi-event-run/v1",
        "run_id": args.run_id,
        "remote_run": remote_run,
        "device": args.device,
        "operator": args.operator,
        "snake": snake,
        "task_rel": task_rel,
        "case_id": case_id,
        "event_timing": {
            "warmup": args.warmup,
            "outer_samples": args.repeat,
            "inner_repeat": args.inner_repeat,
            "cache_mode": args.event_cache_mode,
            "profiler_enabled": False,
            "timer": "npu_event",
        },
        "source_tree_sha256": source_hashes,
        "sequence": sequence,
        "precheck": precheck_text,
    }
    summary = _write_local_results(
        args.artifact_dir, metadata=metadata, sequence=sequence
    )
    print(f"ARTIFACTS {args.artifact_dir}", flush=True)
    for comparison in summary["comparisons"]:
        print(
            f"RESULT arm={comparison['arm']} "
            f"base_p50_us={comparison['base_p50_us']:.6f} "
            f"candidate_p50_us={comparison['candidate_p50_us']:.6f} "
            f"speedup={comparison['speedup']:.6f} "
            f"improved={comparison['improved']}",
            flush=True,
        )

    failed = [entry for entry in sequence if entry.get("status") != "ok"]
    artifact_errors = summary.get("artifact_errors") or {}
    if not failed and not artifact_errors:
        return 0
    if any(entry["role"] == "base" for entry in failed) or "base" in artifact_errors:
        return 2
    return 3


def main(argv: list[str] | None = None) -> int:
    try:
        return _main_impl(argv)
    finally:
        ab._release_active_remote_lock()


if __name__ == "__main__":
    raise SystemExit(main())
