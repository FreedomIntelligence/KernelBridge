"""Recoverable execution, compile-once identities, and legacy tool adapters."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import BuildIdentity, ContractError, ExperimentPhase, content_id
from .store import ArtifactRef, ContentAddressedStore, Ledger

_SECRET_RE = re.compile(r"token|secret|password|cookie|authorization|credential", re.I)
_TERMINAL_MARKER_RE = re.compile(r"UNIFIED_FEEDBACK\s+status=(\S+)\s+path=(.+)$")


@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 1800.0
    required_files: tuple[str, ...] = field(default_factory=tuple)
    required_json_paths: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    require_terminal_marker: bool = False

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0]:
            raise ContractError("command argv cannot be empty")
        if self.timeout_seconds <= 0:
            raise ContractError("command timeout must be positive")
        if any("\x00" in item for item in self.argv):
            raise ContractError("NUL byte in command argument")
        for key in self.env:
            if _SECRET_RE.search(key):
                raise ContractError(f"secret environment key cannot be persisted: {key}")

    @property
    def request_id(self) -> str:
        return content_id("command", self)

    def display(self) -> str:
        return shlex.join(self.argv)


@dataclass(frozen=True)
class CommandResult:
    request_id: str
    status: str
    returncode: int | None
    started_at: float
    finished_at: float
    stdout_ref: str
    stderr_ref: str
    produced_files: Mapping[str, str]
    failure_reason: str | None = None
    terminal_marker: Mapping[str, str] | None = None

    @property
    def elapsed_seconds(self) -> float:
        return self.finished_at - self.started_at

    def compact(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "returncode": self.returncode,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "failure_reason": self.failure_reason,
            "produced_files": dict(self.produced_files),
            "terminal_marker": dict(self.terminal_marker) if self.terminal_marker else None,
            "stdout_ref": self.stdout_ref,
            "stderr_ref": self.stderr_ref,
        }


class CommandRunner:
    """Execute without a shell and retain complete stdout/stderr in CAS."""

    def __init__(self, cas: ContentAddressedStore, ledger: Ledger | None = None):
        self.cas = cas
        self.ledger = ledger

    def _record(self, ref: ArtifactRef) -> str:
        if self.ledger:
            self.ledger.record_artifact(ref)
        return ref.uri

    @staticmethod
    def _resolve(cwd: Path, value: str) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (cwd / path).resolve()

    def run(self, request: CommandRequest, *, inherited_env: Mapping[str, str] | None = None) -> CommandResult:
        cwd = Path(request.cwd).expanduser().resolve()
        if not cwd.is_dir():
            raise NotADirectoryError(cwd)
        environment = dict(os.environ if inherited_env is None else inherited_env)
        environment.update(request.env)
        started = time.time()
        try:
            completed = subprocess.run(
                request.argv,
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=False,
                shell=False,
                timeout=request.timeout_seconds,
                check=False,
            )
            stdout = completed.stdout or b""
            stderr = completed.stderr or b""
            returncode: int | None = completed.returncode
            status = "succeeded" if completed.returncode == 0 else "failed"
            failure = None if completed.returncode == 0 else f"process exited with {completed.returncode}"
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            returncode = None
            status = "timed_out"
            failure = f"timeout after {request.timeout_seconds:.3f}s"
        finished = time.time()
        stdout_ref = self._record(self.cas.put_bytes(stdout, media_type="text/plain; charset=utf-8"))
        stderr_ref = self._record(self.cas.put_bytes(stderr, media_type="text/plain; charset=utf-8"))

        produced: dict[str, str] = {}
        missing: list[str] = []
        for requested_path in request.required_files:
            path = self._resolve(cwd, requested_path)
            if not path.is_file():
                missing.append(requested_path)
                continue
            produced[requested_path] = self._record(self.cas.put_file(path))

        json_failures: list[str] = []
        for requested_path, dotted_path in request.required_json_paths:
            path = self._resolve(cwd, requested_path)
            if not path.is_file():
                json_failures.append(f"missing JSON {requested_path}")
                continue
            try:
                value: Any = json.loads(path.read_text(encoding="utf-8"))
                for segment in dotted_path.split("."):
                    if not isinstance(value, Mapping):
                        raise KeyError(segment)
                    value = value[segment]
                if value in (None, "", [], {}):
                    raise ValueError("value is empty")
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                json_failures.append(f"{requested_path}:{dotted_path}: {exc}")

        decoded = stdout.decode("utf-8", errors="replace")
        marker = None
        for line in reversed(decoded.splitlines()):
            match = _TERMINAL_MARKER_RE.search(line.strip())
            if match:
                marker = {"status": match.group(1), "path": match.group(2).strip()}
                break

        contract_failures: list[str] = []
        if missing:
            contract_failures.append("missing required files: " + ", ".join(missing))
        if json_failures:
            contract_failures.append("invalid required JSON: " + "; ".join(json_failures))
        if request.require_terminal_marker and marker is None:
            contract_failures.append("missing UNIFIED_FEEDBACK terminal marker")
        if status == "succeeded" and contract_failures:
            status = "failed"
            failure = "; ".join(contract_failures)

        return CommandResult(
            request_id=request.request_id,
            status=status,
            returncode=returncode,
            started_at=started,
            finished_at=finished,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            produced_files=produced,
            failure_reason=failure,
            terminal_marker=marker,
        )


@dataclass(frozen=True)
class CompiledArm:
    build: BuildIdentity
    manifest_ref: str
    artifact_root: str
    compiler_log_ref: str

    @property
    def build_id(self) -> str:
        return self.build.build_id


@dataclass(frozen=True)
class UnifiedEvaluationPlan:
    """Compile both arms once, then reuse those exact artifacts everywhere."""

    base: CompiledArm
    candidate: CompiledArm
    event_cases: tuple[str, ...]
    pmu_case: str | None
    pmu_metric_groups: tuple[str, ...]
    repeat: int
    inner_repeat: int

    def __post_init__(self) -> None:
        if not self.event_cases:
            raise ContractError("unified evaluation requires Event cases")
        if self.repeat < 1 or self.inner_repeat < 1:
            raise ContractError("repeat counts must be positive")
        if self.pmu_metric_groups and self.pmu_case is None:
            raise ContractError("PMU groups require a directed PMU case")

    @property
    def plan_id(self) -> str:
        return content_id("unified_eval", self)

    def provenance(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "compile_count": 2,
            "base_build_id": self.base.build_id,
            "candidate_build_id": self.candidate.build_id,
            "event_reuses_builds": True,
            "pmu_reuses_builds": bool(self.pmu_metric_groups),
        }


class LegacyToolAdapter:
    """Build validated requests for existing tools; shadow mode is default."""

    def __init__(self, repo_root: str | Path):
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.pipeline = self.repo_root / "native"
        self.tools = self.pipeline / "r950_tools"
        required = [self.tools / "abctl", self.tools / "real_profiler_ab.py", self.tools / "push910.py"]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError("legacy tool surface incomplete: " + ", ".join(missing))

    @staticmethod
    def _source(path: str | Path) -> str:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise NotADirectoryError(resolved)
        return str(resolved)

    def ab20(
        self,
        *,
        operator_display: str,
        base_source: str | Path,
        candidate_source: str | Path,
        tag: str,
        backend: str = "demo2",
        npu: int = 2,
    ) -> CommandRequest:
        if backend not in {"demo2", "ktp"}:
            raise ContractError(f"unsupported A/B backend: {backend}")
        return CommandRequest(
            argv=(
                str(self.tools / "abctl"),
                "ab",
                "--backend",
                backend,
                "--tag",
                tag,
                operator_display,
                str(npu),
                f"base:{self._source(base_source)}",
                f"mt:{self._source(candidate_source)}",
            ),
            cwd=str(self.tools),
            timeout_seconds=2400.0,
        )

    def profiler(
        self,
        *,
        base_source: str | Path,
        candidate_source: str | Path,
        artifact_dir: str | Path,
        run_id: str,
        device: int,
        case_id: str,
        repeat: int = 30,
        inner_repeat: int = 50,
        metric_groups: Sequence[str] = ("PipeUtilization",),
        action_cell: str = "",
        school_env: str | Path | None = None,
    ) -> CommandRequest:
        artifact = Path(artifact_dir).expanduser().resolve()
        argv: list[str] = [
            sys.executable,
            str(self.tools / "real_profiler_ab.py"),
            "--base-source",
            self._source(base_source),
            "--candidate-source",
            self._source(candidate_source),
            "--artifact-dir",
            str(artifact),
            "--run-id",
            run_id,
            "--device",
            str(device),
            "--case-id",
            str(case_id),
            "--repeat",
            str(repeat),
            "--inner-repeat",
            str(inner_repeat),
            "--event-cache-mode",
            "steady",
        ]
        for metric in metric_groups:
            argv.extend(("--metric", metric))
        if action_cell:
            argv.extend(("--action-cell", action_cell))
        if school_env is not None:
            argv.extend(("--school-env", str(Path(school_env).expanduser().resolve())))
        unified = artifact / "unified_feedback.json"
        return CommandRequest(
            argv=tuple(argv),
            cwd=str(self.tools),
            timeout_seconds=3600.0,
            required_files=(str(unified),),
            required_json_paths=((str(unified), "status"),),
            require_terminal_marker=True,
        )

    def official_submit(self, *, operator: str, source: str | Path, note: str = "") -> CommandRequest:
        source_path = self._source(source)
        spec = f"{operator}:{source_path}" + (f":{note}" if note else "")
        return CommandRequest(
            argv=(sys.executable, str(self.tools / "push910.py"), spec),
            cwd=str(self.tools),
            timeout_seconds=2400.0,
        )

    def shadow_plan(self, request: CommandRequest) -> dict[str, Any]:
        return {
            "mode": "shadow",
            "request_id": request.request_id,
            "argv": list(request.argv),
            "cwd": request.cwd,
            "timeout_seconds": request.timeout_seconds,
            "required_files": list(request.required_files),
            "require_terminal_marker": request.require_terminal_marker,
        }


def require_nonempty_runs(payload: Mapping[str, Any], field: str = "runs") -> Sequence[Any]:
    runs = payload.get(field)
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)) or not runs:
        raise ContractError(f"{field} is empty; this is an execution failure, not a neutral result")
    return runs


def terminal_phase_for_status(status: str) -> ExperimentPhase | None:
    return {
        "compile": ExperimentPhase.COMPILE,
        "correctness": ExperimentPhase.CORRECTNESS,
        "event_ab": ExperimentPhase.EVENT_AB,
        "pmu": ExperimentPhase.PMU,
        "official_standard": ExperimentPhase.OFFICIAL_STANDARD,
        "official_hidden": ExperimentPhase.OFFICIAL_HIDDEN,
    }.get(status)
