"""Admitted finite Dequant build/check/private-event job; stdlib-only on import."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import math
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

Json = dict[str, Any]
PROTOCOL = "kernelcat.dequant.private-warm-events/v1"
CASE_IDS = [f"level3/dequant_swiglu_quant_{number}" for number in range(1, 21)]
LOCK = "/tmp/dq_independent_eval_20261025.lock"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sample_plan(job: Json) -> list[Json]:
    """Twelve balanced blocks. Selection seeds are even; confirmation seeds are odd."""
    request = job["adapter_request"]
    protocol = job["configuration"]["protocol"]
    confirmation = request["operation"] == "confirm"
    domain = protocol["confirmation_seed" if confirmation else "selection_seed"]
    key = [domain, request["task_id"], request["experiment_id"], request["unit_id"],
           request["sample_namespace"]]
    base = int(sha256(canonical(key).encode())[:16], 16) % (2**30 - 12)
    orders = [["keep", "remove"] for _ in range(6)] + [["remove", "keep"] for _ in range(6)]
    random.Random(base).shuffle(orders)
    phase = "confirmation" if confirmation else "selection"
    namespace = sha256(canonical([key, phase]).encode())
    return [{"sample_id": f"dequant:{namespace}:{index:02d}",
             "seed": 2 * (base + index) + int(confirmation), "order": order}
            for index, order in enumerate(orders)]


def _write(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def _framework_revision(root: Path) -> Json:
    executable = shutil.which("git", path="/usr/bin:/bin:/usr/local/bin")
    revision: str | None = None
    reason: str | None = "Git executable unavailable"
    if executable is not None:
        try:
            result = subprocess.run([executable, "-c", "safe.directory=" + str(root),
                "-C", str(root), "rev-parse", "HEAD"], capture_output=True, timeout=15,
                env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
                     "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                     "GIT_CEILING_DIRECTORIES": str(root.parent)})
        except (OSError, subprocess.SubprocessError) as error:
            reason = f"{type(error).__name__}: {error}"
        else:
            observed = result.stdout.decode("utf-8", errors="replace").strip()
            if result.returncode:
                reason = f"Git exited {result.returncode}: " + result.stderr.decode("utf-8", errors="replace").strip()
            elif re.fullmatch(r"[0-9a-f]{40}", observed) is None:
                reason = "Git returned an invalid HEAD revision"
            else:
                revision = observed
                reason = None
    return {"framework_revision": revision,
            "framework_revision_status": "observed" if revision is not None else "unavailable",
            "framework_revision_reason": reason}


def verify_framework(config: Json) -> Json:
    framework = config["framework"]
    root = Path(framework["root"])
    declared = {pin["path"] for pin in framework["files"]}
    actual = {path.relative_to(root).as_posix() for path in (root / "src").rglob("*.py")}
    if actual != {name for name in declared if name.startswith("src/") and name.endswith(".py")}:
        raise ValueError("Dequant framework Python inventory differs from protected pins")
    for pin in framework["files"]:
        path = root / pin["path"]
        if path.is_symlink() or not path.is_file() or sha256(path.read_bytes()) != pin["sha256"]:
            raise ValueError("Dequant original framework/checker/contract changed: " + pin["path"])
    provenance = _framework_revision(root)
    if framework["revision"] is not None and provenance["framework_revision"] is None:
        raise ValueError("Dequant required framework revision unavailable: " + provenance["framework_revision_reason"])
    if framework["revision"] is not None and provenance["framework_revision"] != framework["revision"]:
        raise ValueError("Dequant original framework revision changed")
    return {**provenance, "framework_pins_sha256": sha256(canonical(framework["files"]).encode())}


def _verify_staged(config: Json, root: Path) -> dict[str, bytes]:
    for group, key, name in (("template", "archive", "template.zip"),
                             ("template", "manifest", "template.json"),
                             ("helpers", "build", "build.py"),
                             ("helpers", "measurement_profile", "measurement_profile.json"),
                             ("helpers", "official", "official.py"),
                             ("helpers", "score_report", "score_report.py")):
        if sha256((root / name).read_bytes()) != config[group][key]["sha256"]:
            raise ValueError("Dequant staged protected bytes changed: " + name)
    manifest = json.loads((root / "template.json").read_bytes())
    if manifest["source_archive_sha256"] != config["template"]["archive"]["sha256"]:
        raise ValueError("Dequant original manifest/archive identity changed")
    sources = {}
    with zipfile.ZipFile(root / "template.zip") as archive:
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or str(path) != entry.filename or ".." in path.parts
                    or ".git" in path.parts or entry.is_dir() or entry.filename in sources
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Dequant source template contains an unsafe file")
            sources[entry.filename] = archive.read(entry)
    if {name: sha256(data) for name, data in sources.items()} != manifest["source_hashes"]:
        raise ValueError("Dequant source template differs from original clean manifest")
    profile = json.loads((root / "measurement_profile.json").read_bytes())
    if (profile["framework"] != config["framework"]["root"] or profile["device_id"] != config["protocol"]["device_id"]
            or profile["warmup"] != 3 or profile["repeat"] != 5 or profile["processes_per_card"] != 1
            or profile["entrypoint"] != "kernel_eval.staged_eval"):
        raise ValueError("Dequant route profile must bind the same device and original w3/r5 protocol")
    return sources


def _environment(config: Json, helper_directory: Path, package: Path | None = None) -> dict[str, str]:
    root = Path(config["framework"]["root"])
    imports = [root / "src", helper_directory] + ([package] if package is not None else [])
    return dict(os.environ, TORCH_DEVICE_BACKEND_AUTOLOAD="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
        OMP_NUM_THREADS="8", MKL_NUM_THREADS="8", OPENBLAS_NUM_THREADS="8", NUMEXPR_NUM_THREADS="8",
        MALLOC_ARENA_MAX="4", CANN_BENCH_SCORE_WEIGHTS="2", CANN_BENCH_PERF_METRIC="eq3",
        ASCEND_HOME_PATH="/usr/local/Ascend/cann-9.1.0", NPU_ARCH="ascend910_93",
        PYTHONPATH=os.pathsep.join(map(str, imports)), PATH=str(Path(sys.executable).parent) + ":" + os.environ["PATH"])


def _run(argv: list[str], root: Path, environment: dict[str, str], log: Path, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Dequant admitted job exceeded its finite deadline")
    with log.open("xb") as output:
        result = subprocess.run(argv, cwd=root, env=environment, stdout=output,
                                stderr=subprocess.STDOUT, timeout=remaining)
    if result.returncode:
        raise RuntimeError(f"Dequant command failed ({result.returncode}); retained log: {log.name}")


def _build(job: Json, sources: dict[str, bytes], arm: str, root: Path, deadline: float) -> Json:
    project = root / "projects" / arm
    project.mkdir(parents=True, exist_ok=False)
    expected = dict(sources)
    for item in job["adapter_request"][arm]:
        expected[item["path"]] = item["text"].encode()
    for name, data in expected.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    environment = _environment(job["configuration"], root)
    environment["DQ_BUILD_PROJECT"] = "projects/" + arm
    _run([sys.executable, str(root / "build.py"), arm], root, environment,
         root / ("build-" + arm + ".log"), deadline)
    manifest_path = root / "sources" / (arm + ".json")
    manifest = json.loads(manifest_path.read_bytes())
    if manifest["source_hashes"] != {name: sha256(data) for name, data in expected.items()}:
        raise ValueError("Dequant freshly built source inventory differs from frozen " + arm)
    binaries = list((root / arm / "cann_bench").glob("_C*.so"))
    if len(binaries) != 1 or sha256(binaries[0].read_bytes()) != manifest["binary_sha256"]:
        raise ValueError("Dequant measured binary differs from fresh build: " + arm)
    if manifest["rebuild_all_tus"] is not True or not manifest["compile_commands"]:
        raise ValueError("Dequant build omitted fresh compilation evidence: " + arm)
    return {"source_commit": job["snapshot"][arm]["commit"],
            "source_digest": job["snapshot"][arm]["source_digest"],
            "binary_sha256": manifest["binary_sha256"],
            "source_archive_sha256": manifest["source_archive_sha256"],
            "compile_commands_sha256": sha256(canonical(manifest["compile_commands"]).encode()),
            "manifest": str(manifest_path.relative_to(root)),
            "manifest_sha256": sha256(manifest_path.read_bytes()), "fresh_build": True}


def profile_observation(report: Json) -> tuple[Json, Json]:
    """Read actual profiler kernel names without inferring launch plans from source."""
    operators = report["operators"]
    if len(operators) != 1 or report["result_stage"] != "final" or report["contains_performance"] is not True:
        raise ValueError("Dequant original profiler returned the wrong report scope")
    rows = operators[0]["cases"]
    correctness = {row["case_id"]: row["status"] == "success" and row["accuracy"]["passed"] is True for row in rows}
    if len(rows) != 20 or set(correctness) != set(CASE_IDS) or not all(correctness.values()):
        raise ValueError("Dequant original checker/profiler did not pass all twenty public cases")
    routes = {}
    for row in rows:
        kernels = row["op_times"].get("device_kernels") if row["op_times"] is not None else None
        routes[row["case_id"]] = {"kernel_names": sorted(kernels) if kernels else None,
            "final_core_plan": None,
            "reason": "Original profiler kernel names retained; report does not expose final cores/batch/PC"}
    return {"cases": correctness}, routes


def _profile(job: Json, arm: str, root: Path, deadline: float) -> tuple[Json, Json, Json]:
    label = "profile_" + arm
    command = [sys.executable, str(root / "official.py"), label, arm]
    _run(command, root, _environment(job["configuration"], root, root / arm),
         root / ("profile-" + arm + ".log"), deadline)
    report_path = root / "official" / label / (label + "_final.json")
    correctness, routes = profile_observation(json.loads(report_path.read_bytes()))
    correctness.update(report=str(report_path.relative_to(root)), report_sha256=sha256(report_path.read_bytes()),
                       command=command, eval_seed=0)
    summary = json.loads((root / (label + "_summary.json")).read_bytes())
    return correctness, routes, summary


def _observe_environment(job: Json, root: Path, name: str, deadline: float) -> Json:
    output = root / (name + ".json")
    _run([sys.executable, str(root / "dequant_job.py"), "--environment", str(root / "experiment-request.json"),
          str(output)], root, _environment(job["configuration"], root), root / (name + ".log"), deadline)
    return cast(Json, json.loads(output.read_bytes()))


def _measure(job: Json, root: Path, deadline: float) -> list[Json]:
    blocks = []
    directory = root / "events"
    directory.mkdir(exist_ok=False)
    for index, block in enumerate(sample_plan(job)):
        observed = dict(block)
        for arm in block["order"]:
            output = directory / f"{index:02d}-{arm}.json"
            _run([sys.executable, str(root / "dequant_job.py"), "--sample",
                str(root / "experiment-request.json"), arm, str(block["seed"]), str(output)],
                root, _environment(job["configuration"], root, root / arm), output.with_suffix(".log"), deadline)
            observed[arm] = json.loads(output.read_bytes())
        blocks.append(observed)
        _write(directory / f"{index:02d}-pair.json", observed)
    return blocks


def _acquire(lock: Any, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Dequant finite wait for original device lock expired")
            time.sleep(min(0.2, remaining))


def contributions(blocks: list[Json]) -> Json:
    result = {}
    for case_id in CASE_IDS:
        averages = {arm: [sum(block[arm][case_id]) / len(block[arm][case_id]) for block in blocks]
                    for arm in ("keep", "remove")}
        deltas = [math.log(remove / keep) for keep, remove in zip(averages["keep"], averages["remove"], strict=True)]
        result[case_id] = {"keep_mean_us": sum(averages["keep"]) / len(blocks),
            "remove_mean_us": sum(averages["remove"]) / len(blocks),
            "mean_log_speedup": sum(deltas) / len(blocks),
            "mean_log_score_contribution": sum(deltas) / len(blocks) / len(CASE_IDS)}
    return result


def run_job(job: Json, root: Path) -> Json:
    config = job["configuration"]
    if (job["schema_version"] != "kernelcat.dequant-job/v1" or config["protocol"]["id"] != PROTOCOL
            or config["protocol"]["pairs"] != 12 or job["adapter_request"]["operation"] not in {"measure", "confirm"}):
        raise ValueError("Dequant job requires its fixed finite private protocol")
    if Path(sys.executable).resolve() != Path(config["remote"]["python"]).resolve():
        raise ValueError("Dequant job interpreter differs from admitted Python")
    deadline = time.monotonic() + config["remote"]["timeout"]
    framework_before = verify_framework(config)
    sources = _verify_staged(config, root)
    for item in job["adapter_request"]["baseline"]:
        if sources[item["path"]] != item["text"].encode():
            raise ValueError("Dequant job baseline differs from protected parent archive")
    _write(root / "experiment-request.json", job)
    builds = {arm: _build(job, sources, arm, root, deadline) for arm in ("keep", "remove")}
    environment_before = _observe_environment(job, root, "environment-before", deadline)
    correctness, routes, profile_summaries = {}, {}, {}
    for arm in ("keep", "remove"):
        correctness[arm], routes[arm], profile_summaries[arm] = _profile(job, arm, root, deadline)
    if verify_framework(config) != framework_before:
        raise ValueError("Dequant framework provenance changed after profiler work")
    with open(LOCK, "a") as lock:
        _acquire(lock, deadline)
        event_environment = _observe_environment(job, root, "environment-events", deadline)
        if event_environment != environment_before:
            raise ValueError("Dequant environment changed between profiler and paired Events")
        blocks = _measure(job, root, deadline)
        environment_after = _observe_environment(job, root, "environment-after", deadline)
    if verify_framework(config) != framework_before:
        raise ValueError("Dequant framework provenance changed during paired Events")
    if environment_before != environment_after:
        raise ValueError("Dequant environment changed during the admitted paired experiment")
    return {"schema_version": "kernelcat.dequant-raw/v1", "request_sha256": sha256(canonical(job).encode()),
        "protocol_id": PROTOCOL, "scope": "Private paired NPU Events with separate original profiler route evidence; no website score",
        "environment_before": environment_before, "environment_after": environment_after,
        "source_commits": {arm: job["snapshot"][arm]["commit"] for arm in ("keep", "remove")},
        "builds": builds, "correctness": correctness, "blocks": blocks,
        "case_contributions": contributions(blocks), "profile_summaries": profile_summaries,
        "route": routes}


def _native_environment(job: Json) -> Json:
    config = job["configuration"]
    framework = verify_framework(config)
    torch = importlib.import_module("torch")
    torch_npu = importlib.import_module("torch_npu")

    device = config["protocol"]["device_id"]
    torch.npu.set_device(device)
    driver = Path("/usr/local/Ascend/driver/version.info")
    compiler = Path("/usr/local/Ascend/cann-9.1.0/aarch64-linux/ccec_compiler/bin/bisheng")
    return {"python_executable": str(Path(sys.executable).resolve()), "python": platform.python_version(),
        "python_sha256": sha256(Path(sys.executable).resolve().read_bytes()), "platform": platform.platform(),
        "torch": torch.__version__, "torch_npu": torch_npu.__version__,
        "device_id": device, "device_name": torch.npu.get_device_name(device),
        "visible_device_count": torch.npu.device_count(),
        "device_properties": str(torch.npu.get_device_properties(device)),
        "driver_version_sha256": sha256(driver.read_bytes()) if driver.is_file() else None,
        "compiler_sha256": sha256(compiler.resolve().read_bytes()) if compiler.is_file() else None,
        "cann_home": "/usr/local/Ascend/cann-9.1.0", "architecture": "ascend910_93",
        **framework}


def _event_samples(job: Json, arm: str, seed: int, root: Path) -> Json:
    # Backend registration must precede the linked extension's import.
    torch = importlib.import_module("torch")
    importlib.import_module("torch_npu")
    cann_bench = importlib.import_module("cann_bench")
    loader = importlib.import_module("kernel_eval.benches.cann_loader")
    generation = importlib.import_module("kernel_eval.data.data_generator")
    package_file = cann_bench.__file__
    if package_file is None or Path(package_file).resolve() != (root / arm / "cann_bench" / "__init__.py").resolve():
        raise ValueError("Dequant loaded package differs from the admitted fresh arm")

    config = job["configuration"]
    torch.set_num_threads(4)
    torch.npu.set_device(config["protocol"]["device_id"])
    cases = loader.CannCaseLoader(tasks_root=str(Path(config["framework"]["root"]) / "tasks")).scan_by_rel_path(
        "level3/dequant_swiglu_quant")
    if len(cases) != 20 or {case.get_case_id_str() for case in cases} != set(CASE_IDS):
        raise ValueError("Dequant original case loader did not return exactly the full20 suite")
    random.Random(seed).shuffle(cases)
    generator = generation.DataGenerator(input_dist="uniform")
    result = {}
    with torch.inference_mode():
        for case in cases:
            case_id = case.get_case_id_str()
            case_hash = int.from_bytes(hashlib.sha256(case_id.encode()).digest()[:8], "big") % 2**31
            inputs = generator.generate_input_tensors_from_case(case.input_shapes, case.dtypes,
                case.value_ranges, seed=(seed + case_hash) % 2**31)
            inputs = [value.npu() if value is not None else None for value in inputs]
            arguments = inputs + [None] * (4 - len(inputs))
            arguments.append(case.attrs["activate_left"])
            for _ in range(config["protocol"]["warmup"]):
                cann_bench.dequant_swiglu_quant(*arguments)
            torch.npu.synchronize()
            durations = []
            for _ in range(config["protocol"]["repeat"]):
                start, end = torch.npu.Event(enable_timing=True), torch.npu.Event(enable_timing=True)
                start.record()
                outputs = cann_bench.dequant_swiglu_quant(*arguments)
                end.record()
                end.synchronize()
                elapsed_us = float(start.elapsed_time(end)) * 1000
                if not math.isfinite(elapsed_us) or elapsed_us <= 0:
                    raise ValueError("Dequant NPU Event produced an invalid duration: " + case_id)
                durations.append(elapsed_us)
            result[case_id] = durations
            del outputs, inputs, arguments
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        job = json.loads(Path(arguments[1]).read_bytes())
        with contextlib.redirect_stdout(sys.stderr):
            if arguments[0] == "--environment" and len(arguments) == 3:
                result = _native_environment(job)
            elif arguments[0] == "--sample" and len(arguments) == 5:
                result = _event_samples(job, arguments[2], int(arguments[3]), Path.cwd())
            else:
                raise ValueError("unsupported finite Dequant native subprocess mode")
        _write(Path(arguments[-1]), result)
        return 0
    job = json.load(sys.stdin)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = run_job(job, Path.cwd())
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        # A timed-out observer cannot prove that all native descendants stopped.
        result = {"status": "unknown", "request_sha256": sha256(canonical(job).encode()),
                  "reason": f"{type(error).__name__}: {error}"}
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
