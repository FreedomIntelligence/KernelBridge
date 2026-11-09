"""Finite bridge from admitted RSI Actions to one pinned Dequant experiment."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from kernelcat_executor.contracts.models import Action, Json, Outcome, canonical, digest
from kernelcat_executor.contracts.rsi import validate_rsi

if TYPE_CHECKING:
    from kernelcat_worker.remote_jobs import Gateway, JobResult, RemoteJob, RemoteJobs


PROTOCOL = "kernelcat.dequant.private-warm-events/v1"
OPERATOR_PATH = "csrc/ops/dequant_swiglu_quant"
CASE_IDS = [f"level3/dequant_swiglu_quant_{number}" for number in range(1, 21)]
CONTRACT_FILES = {
    "tasks/level3/dequant_swiglu_quant/golden.py",
    "tasks/level3/dequant_swiglu_quant/cases.yaml",
    "tasks/level3/dequant_swiglu_quant/proto.yaml",
    "tasks/metadata/910c.json",
}


def _object(value: object, names: set[str], label: str) -> Json:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError(f"Dequant {label} requires exactly {sorted(names)}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Dequant requires a nonempty " + label)
    return value


def _integer(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Dequant {label} must be an integer in [{minimum}, {maximum}]")
    return value


def _absolute(value: object) -> str:
    name = _text(value, "absolute path")
    if not PurePosixPath(name).is_absolute() or str(PurePosixPath(name)) != name:
        raise ValueError("Dequant requires a canonical absolute path: " + name)
    return name


def _relative(value: object) -> str:
    name = _text(value, "relative path")
    path = PurePosixPath(name)
    if (path.is_absolute() or str(path) != name or "\\" in name
            or any(part in {"..", ".git"} for part in path.parts)
            or any(character in name for character in "\r\n\t")):
        raise ValueError("Dequant requires a safe relative path: " + name)
    return name


def _hex(value: object, length: int = 64) -> str:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is None:
        raise ValueError("Dequant requires a lowercase content hash")
    return value


def _pin(value: object, *, relative: bool = False) -> Json:
    pin = _object(value, {"path", "sha256"}, "file pin")
    (_relative if relative else _absolute)(pin["path"])
    _hex(pin["sha256"])
    return pin


def read_pinned(pin: Json) -> bytes:
    path = Path(pin["path"])
    if path.resolve() != path or path.is_symlink() or not path.is_file():
        raise ValueError("Dequant pin is not a canonical regular file: " + str(path))
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError("Dequant pinned bytes changed: " + str(path))
    return data


def check_configuration(value: object) -> Json:
    """Validate protected Python configuration, without importing a transport or SDK."""
    names = {"schema_version", "task_id", "source_paths", "gateway", "remote",
             "template", "helpers", "framework", "protocol"}
    if isinstance(value, dict) and "review" in value:
        names.add("review")
    config = _object(value, names, "configuration")
    if config["schema_version"] != "kernelcat.dequant-adapter/v1":
        raise ValueError("unsupported Dequant configuration version")
    _text(config["task_id"], "task_id")
    _pin(config["gateway"])
    _check_source_paths(config["source_paths"])
    remote = _object(config["remote"], {"root", "python", "timeout", "observe_seconds"}, "remote")
    _absolute(remote["root"])
    _absolute(remote["python"])
    _integer(remote["timeout"], 1, 3500, "job timeout")
    _integer(remote["observe_seconds"], 0, 3000, "observation bound")
    for pin in _object(config["template"], {"archive", "manifest"}, "template").values():
        _pin(pin)
    for pin in _object(config["helpers"], {"build", "measurement_profile", "official", "score_report"}, "helpers").values():
        _pin(pin)
    _check_framework(config["framework"])
    _check_protocol(config["protocol"])
    if "review" in config:
        _pin(config["review"])
    return cast(Json, json.loads(canonical(config)))


def _check_source_paths(paths: object) -> None:
    if not isinstance(paths, list) or not 1 <= len(paths) <= 64:
        raise ValueError("Dequant requires between 1 and 64 declared source paths")
    for name in paths:
        path = PurePosixPath(_relative(name))
        if path.parent.as_posix() != OPERATOR_PATH or path.suffix not in {".cpp", ".h"}:
            raise ValueError("Dequant source must be a declared operator .cpp or .h file")
    if len(set(paths)) != len(paths):
        raise ValueError("Dequant repeats a declared source path")


def _check_framework(value: object) -> None:
    framework = _object(value, {"root", "revision", "files"}, "framework")
    _absolute(framework["root"])
    if framework["revision"] is not None:
        _hex(framework["revision"], 40)
    pins = framework["files"]
    if not isinstance(pins, list) or not pins or len(pins) > 4096:
        raise ValueError("Dequant requires bounded framework file pins")
    names = [_pin(pin, relative=True)["path"] for pin in pins]
    if len(set(names)) != len(names) or not CONTRACT_FILES.issubset(names):
        raise ValueError("Dequant requires unique original golden, cases, proto and anchor pins")
    if not any(name.startswith("src/") and name.endswith(".py") for name in names):
        raise ValueError("Dequant requires original framework Python pins")


def _check_protocol(value: object) -> None:
    protocol = _object(value, {"id", "device_id", "warmup", "repeat", "pairs",
                               "selection_seed", "confirmation_seed"}, "protocol")
    if protocol["id"] != PROTOCOL:
        raise ValueError("Dequant private protocol requires exactly twelve paired blocks")
    _integer(protocol["pairs"], 12, 12, "paired block count")
    _integer(protocol["device_id"], 0, 255, "device_id")
    _integer(protocol["warmup"], 1, 100, "warmup")
    _integer(protocol["repeat"], 1, 100, "repeat")
    for name in ("selection_seed", "confirmation_seed"):
        _integer(protocol[name], 0, 2**31 - 1, name)
    if protocol["selection_seed"] == protocol["confirmation_seed"]:
        raise ValueError("Dequant confirmation requires a separate seed domain")


def template_sources(config: Json) -> tuple[bytes, bytes, dict[str, bytes]]:
    archive = read_pinned(config["template"]["archive"])
    manifest_bytes = read_pinned(config["template"]["manifest"])
    manifest = json.loads(manifest_bytes)
    if manifest["source_archive_sha256"] != config["template"]["archive"]["sha256"]:
        raise ValueError("Dequant template archive differs from its original manifest")
    import io

    sources = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        entries = package.infolist()
        if len(entries) > 4096 or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
            raise ValueError("Dequant template exceeds its source archive bound")
        for entry in entries:
            name = _relative(entry.filename)
            if entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Dequant template must contain regular source files")
            if name in sources:
                raise ValueError("Dequant template repeats a source file")
            sources[name] = package.read(entry)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    if hashes != manifest["source_hashes"]:
        raise ValueError("Dequant archive file inventory differs from its original source manifest")
    if not set(config["source_paths"]).issubset(sources):
        raise ValueError("Dequant declared source is absent from its original template")
    return archive, manifest_bytes, sources


def _git(repository: str, *arguments: str) -> bytes:
    executable = shutil.which("git", path="/usr/bin:/bin:/usr/local/bin")
    if executable is None:
        raise ValueError("Dequant source verification requires Git")
    result = subprocess.run([executable, "-c", "core.hooksPath=/dev/null",
        "--git-dir=" + repository, *arguments], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=20, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return result.stdout


def verify_snapshot(request: Json, manifest: Path, paths: list[str]) -> Json:
    """Verify original refs, Git blob identities and clean owner-side checkout bytes."""
    if not manifest.is_absolute() or manifest.resolve() != manifest or manifest.is_symlink():
        raise ValueError("Dequant requires the original canonical source manifest")
    snapshot = validate_rsi(json.loads(manifest.read_bytes()), "RsiSourceSnapshot")
    for arm in ("baseline", "keep", "remove"):
        row = snapshot[arm]
        if row["repository"] != str(manifest.parent / "sources.git"):
            raise ValueError("Dequant source repository differs from original manifest root")
        if row["worktree"] != str(manifest.parent / arm):
            raise ValueError("Dequant source checkout differs from original manifest root")
        if row["source_digest"] != digest(request[arm]):
            raise ValueError("Dequant source digest differs from original request: " + arm)
        files = {item["path"]: item["text"].encode() for item in request[arm]}
        if len(files) != len(request[arm]) or set(files) != set(paths):
            raise ValueError("Dequant request must use exactly its declared source paths")
        _verify_commit(row, arm, files)
    return snapshot


def _verify_commit(row: Json, arm: str, files: dict[str, bytes]) -> None:
    commit = _hex(row["commit"], 40)
    if _git(row["repository"], "rev-parse", "refs/heads/" + arm).decode().strip() != commit:
        raise ValueError("Dequant original source commit changed: " + arm)
    expected = {}
    for name, data in files.items():
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        expected[name] = "100644 blob " + blob
        path = Path(row["worktree"]) / name
        if path.resolve() != path or path.is_symlink() or path.read_bytes() != data:
            raise ValueError("Dequant original source checkout changed: " + arm + "/" + name)
    actual = {}
    for entry in _git(row["repository"], "ls-tree", "-rz", commit).split(b"\0"):
        if entry:
            identity, name_bytes = entry.decode().split("\t", 1)
            actual[name_bytes] = identity
    if actual != expected:
        raise ValueError("Dequant original commit tree differs from source request: " + arm)


def complexity(files: list[Json]) -> Json:
    """Deterministic lexical C++ size/branch counts; these are not compiler AST counts."""
    code = "\n".join(item["text"] for item in files)
    code = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                  " ", code, flags=re.DOTALL)
    tokens = re.findall(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|&&|\|\||[^\s]", code)
    branches = sum(token in {"if", "for", "while", "switch", "case", "catch", "?"} for token in tokens)
    depth = cognitive = 0
    for token in tokens:
        if token == "{":
            depth += 1
        elif token == "}":
            depth = max(0, depth - 1)
        elif token in {"if", "for", "while", "switch", "case", "catch", "?"}:
            cognitive += 1 + depth
    return {"nodes": len(tokens), "branches": branches, "specializations": tokens.count("template"),
            "cognitive": cognitive, "configuration": len(re.findall(r"(?m)^\s*#\s*(?:if|ifdef|ifndef)\b", code))}


def decompose(request: Json) -> Json:
    before = {item["path"]: item["text"] for item in request["baseline"]}
    units = []
    for item in sorted(request["keep"], key=lambda value: value["path"]):
        if item["text"] == before[item["path"]]:
            continue
        units.append({"unit_id": "dequant-source-" + digest(item["path"])[:24],
            "anchor": "whole-file:" + item["path"], "path": item["path"],
            "before": before[item["path"]], "after": item["text"], "dependencies": [], "ku_ids": []})
    return validate_rsi({"decomposition": {"units": units,
        "candidate_complexity": complexity(request["keep"]),
        "baseline_complexity": complexity(request["baseline"])}}, "AdapterResult")


def job_request(config: Json, action: Action, snapshot: Json) -> Json:
    request = validate_rsi(action.arguments, "AdapterRequest")
    if action.task != config["task_id"] or request["task_id"] != action.task:
        raise ValueError("Dequant Action, request and configured task must agree")
    return {"schema_version": "kernelcat.dequant-job/v1", "action_id": action.id,
            "adapter_request": request, "snapshot": snapshot, "configuration": config}


def _sample_plan(job: Json) -> list[Json]:
    from kernelcat_worker.dequant_job import sample_plan

    return sample_plan(job)


def paired_measurement(raw: Json, job: Json, handle: RemoteJob) -> Json:
    """Convert raw full20 blocks, refusing omissions, seed reuse or changed identities."""
    request = job["adapter_request"]
    if (raw["schema_version"] != "kernelcat.dequant-raw/v1"
            or raw["request_sha256"] != hashlib.sha256(canonical(job).encode()).hexdigest()
            or raw["protocol_id"] != PROTOCOL
            or raw["source_commits"] != {arm: job["snapshot"][arm]["commit"] for arm in ("keep", "remove")}):
        raise ValueError("Dequant raw result identity differs from original job")
    if raw["environment_before"] != raw["environment_after"]:
        raise ValueError("Dequant measured environment changed during paired blocks")
    _verify_raw_builds(raw, job)
    correctness = {arm: _correctness(raw["correctness"][arm]) for arm in ("keep", "remove")}
    blocks = raw["blocks"]
    plan = _sample_plan(job)
    if len(blocks) != len(plan):
        raise ValueError("Dequant requires all twelve independent paired blocks")
    scores: dict[str, list[float]] = {"keep": [], "remove": []}
    for block, expected in zip(blocks, plan, strict=True):
        if {key: block[key] for key in expected} != expected:
            raise ValueError("Dequant paired sample identity/order/seed changed")
        for arm in scores:
            times = _case_times(block[arm], job["configuration"]["protocol"]["repeat"])
            scores[arm].append(-sum(math.log(value) for value in times) / len(CASE_IDS))
    evidence = "dequant-raw:sha256:" + hashlib.sha256(canonical(raw).encode()).hexdigest()
    result = {"measurement_id": "dequant:" + digest([handle.identity, request["operation"]]),
        "task_id": request["task_id"], "environment_id": "sha256:" + digest(raw["environment_before"]),
        "protocol_id": PROTOCOL + ":" + digest(job["configuration"]["protocol"]),
        "keep_digest": digest(request["keep"]), "remove_digest": digest(request["remove"]),
        "sample_ids": [block["sample_id"] for block in blocks],
        "keep_log_scores": scores["keep"], "remove_log_scores": scores["remove"],
        "keep_correct": correctness["keep"], "remove_correct": correctness["remove"],
        "independent_confirmation": request["operation"] == "confirm",
        "evidence_ids": [evidence, "remote-job:" + handle.directory],
        "features": {"surface": ["cpp-lexical-complexity/v1"], "lowered": None, "runtime": None},
        "remove_features": {"surface": ["cpp-lexical-complexity/v1"], "lowered": None, "runtime": None},
        "visible_coverage": CASE_IDS, "holdout_coverage": None,
        "keep_complexity": complexity(request["keep"]), "remove_complexity": complexity(request["remove"])}
    return validate_rsi({"measurement": result}, "AdapterResult")


def _verify_raw_builds(raw: Json, job: Json) -> None:
    for arm in ("keep", "remove"):
        build = raw["builds"][arm]
        snapshot = job["snapshot"][arm]
        if (build["source_commit"] != snapshot["commit"]
                or build["source_digest"] != snapshot["source_digest"] or build["fresh_build"] is not True):
            raise ValueError("Dequant build differs from the original source commit: " + arm)
        for field in ("binary_sha256", "source_archive_sha256", "compile_commands_sha256", "manifest_sha256"):
            _hex(build[field])
        _relative(build["manifest"])


def _correctness(value: Json) -> bool | None:
    rows = value["cases"]
    if set(rows) != set(CASE_IDS) or any(type(flag) not in {bool, type(None)} for flag in rows.values()):
        raise ValueError("Dequant correctness requires all twenty explicit case observations")
    if any(flag is False for flag in rows.values()):
        return False
    return None if any(flag is None for flag in rows.values()) else True


def _case_times(value: Json, repeat: int) -> list[float]:
    if set(value) != set(CASE_IDS):
        raise ValueError("Dequant paired block does not cover the full twenty cases")
    result = []
    for case_id in CASE_IDS:
        samples = value[case_id]
        if (not isinstance(samples, list) or len(samples) != repeat
                or any(type(sample) not in {int, float} or not math.isfinite(sample) or sample <= 0
                       for sample in samples)):
            raise ValueError("Dequant requires positive finite raw NPU Event times for " + case_id)
        result.append(sum(samples) / repeat)
    return result


class DequantAdapter:
    """One finite admitted operation. RemoteJobs alone owns remote launch reconciliation."""

    def __init__(self, config: Json, jobs: RemoteJobs) -> None:
        self.config = check_configuration(config)
        self.jobs = jobs

    def execute(self, action: Action, manifest: Path, *, collect: bool = False,
                status: bool = False) -> Outcome:
        from kernelcat_worker.remote_jobs import (
            RemoteJob,
            RemoteJobConflict,
            RemoteJobProtocolError,
            RemoteJobUncertain,
        )

        request = validate_rsi(action.arguments, "AdapterRequest")
        snapshot = verify_snapshot(request, manifest, self.config["source_paths"])
        job = job_request(self.config, action, snapshot)
        if request["operation"] == "decompose":
            if collect or status:
                raise ValueError("Dequant decomposition has no remote job to collect")
            self._stage(job)
            return Outcome(data={"rsi": decompose(request)})
        handle = RemoteJob(action.id, hashlib.sha256(canonical(job).encode()).hexdigest(),
            str(PurePosixPath(self.config["remote"]["root"]) / hashlib.sha256(action.id.encode()).hexdigest()))
        try:
            if collect or status:
                result = self.jobs.status(handle) if status else self.jobs.collect(handle)
            else:
                files = self._stage(job)
                handle = self.jobs.submit(action.id, job, files, ["dequant_job.py"])
                result = self._observe(handle)
        except RemoteJobConflict as error:
            return Outcome(status="unknown", data={"reason": error.reason,
                "remote_status": "conflict", "remote_handle": asdict(error.handle),
                "native_status": "unknown", "reservation_status": "retained"})
        except (RemoteJobUncertain, RemoteJobProtocolError) as error:
            return Outcome(status="unknown", data={"reason": error.reason,
                                                    "remote_status": "unknown",
                                                    "remote_handle": asdict(error.handle)})
        return self._outcome(result, job)

    def _stage(self, job: Json) -> dict[str, bytes]:
        if "review" in self.config:
            _reviewed_sources(json.loads(read_pinned(self.config["review"])), job["adapter_request"])
        archive, manifest_bytes, sources = template_sources(self.config)
        for item in job["adapter_request"]["baseline"]:
            if item["text"].encode() != sources[item["path"]]:
                raise ValueError("Dequant baseline differs from its frozen original template")
        helper_files = {name: read_pinned(self.config["helpers"][key]) for key, name in (
            ("build", "build.py"), ("measurement_profile", "measurement_profile.json"),
            ("official", "official.py"), ("score_report", "score_report.py"))}
        return {"dequant_job.py": Path(__file__).with_name("dequant_job.py").read_bytes(),
                "template.zip": archive, "template.json": manifest_bytes, **helper_files}

    def _observe(self, handle: RemoteJob) -> JobResult:
        deadline = time.monotonic() + self.config["remote"]["observe_seconds"]
        while True:
            result = self.jobs.collect(handle)
            if result.status != "running" or time.monotonic() >= deadline:
                return result
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    @staticmethod
    def _outcome(result: JobResult, job: Json) -> Outcome:
        if result.status != "completed":
            return Outcome(status="failed" if result.status == "failed" else "unknown",
                data={"reason": result.reason or "remote_job_" + result.status,
                      "remote_status": result.status, "remote_handle": asdict(result.handle)})
        if result.result is None:
            raise ValueError("Dequant completed job omitted its raw measurement")
        if result.result.get("status") == "unknown":
            if result.result["request_sha256"] != result.handle.request_sha256:
                raise ValueError("Dequant uncertain native result has a different original request")
            return Outcome(status="unknown", data={"reason": result.result["reason"],
                "remote_status": "unknown", "remote_handle": asdict(result.handle)})
        measured = paired_measurement(result.result, job, result.handle)
        return Outcome(data={"rsi": measured, "remote_handle": asdict(result.handle)})


def _reviewed_sources(value: object, request: Json) -> None:
    receipt = _object(value, {"schema_version", "task_id", "baseline_digest", "candidate_digest",
                              "approved", "reviewer_id", "evidence_ids"}, "review receipt")
    if (receipt["schema_version"] != "kernelcat.dequant-review/v1" or receipt["approved"] is not True
            or receipt["task_id"] != request["task_id"] or receipt["baseline_digest"] != digest(request["baseline"])):
        raise ValueError("Dequant independent review did not approve this original task/parent")
    _text(receipt["reviewer_id"], "reviewer identity")
    if not isinstance(receipt["evidence_ids"], list) or not 1 <= len(receipt["evidence_ids"]) <= 256:
        raise ValueError("Dequant independent review requires bounded original evidence references")
    for evidence in receipt["evidence_ids"]:
        _text(evidence, "review evidence reference")
    approved = {_hex(receipt["baseline_digest"]), _hex(receipt["candidate_digest"])}
    if any(digest(request[arm]) not in approved for arm in ("keep", "remove")):
        raise ValueError("Dequant source variant is outside the pinned independent review")


def _gateway(config: Json) -> Gateway:
    read_pinned(config["gateway"])
    os.environ["PATH"] = "/usr/bin:/bin:/usr/local/bin"
    spec = importlib.util.spec_from_file_location("kernelcat_dequant_gateway", config["gateway"]["path"])
    if spec is None or spec.loader is None:
        raise ValueError("Dequant cannot load its pinned gateway helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", nargs="?")
    parser.add_argument("--config")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--collect", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    config_path = args.config or args.configuration
    if config_path is None or (args.config and args.configuration):
        parser.error("supply one absolute configuration path")
    try:
        path = Path(_absolute(config_path))
        if path.resolve() != path or path.is_symlink() or not path.is_file():
            raise ValueError("Dequant configuration must be a canonical regular file")
        config = check_configuration(json.loads(path.read_bytes()))
        action = Action.model_validate_json(sys.stdin.buffer.read())
        manifest = Path(os.environ["KERNELCAT_RSI_SOURCE_MANIFEST"])
        from kernelcat_worker.remote_jobs import RemoteJobs

        jobs = RemoteJobs(_gateway(config), config["remote"]["root"], config["remote"]["python"])
        outcome = DequantAdapter(config, jobs).execute(action, manifest, collect=args.collect, status=args.status)
        print(canonical(outcome))
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        outcome = Outcome(status="unknown" if args.collect or args.status else "failed",
                          data={"reason": f"{type(error).__name__}: {error}"})
        print(canonical(outcome))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
