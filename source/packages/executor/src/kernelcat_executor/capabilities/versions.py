"""Finite, local observations; no model requests, discovery, installs or retries."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import urlsplit

from kernelcat_executor.capabilities.process import ProcessResult, collect_process
from kernelcat_executor.contracts.models import Json, digest
from kernelcat_executor.contracts.version_lock import unknown as unknown
from kernelcat_executor.generated.version_lock import (
    ExecutableIdentity,
    KnownIdentity,
    KnownSource,
    SourceIdentity,
    VersionProbe,
)


def known(value: str, kind: Literal["runtime", "installed_metadata", "executable",
                                  "configuration", "source", "owner_receipt"],
          evidence_digest: str) -> KnownIdentity:
    return {"state": "known", "value": value,
            "evidence": {"kind": kind, "digest": evidence_digest}}


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ExecutableObservation:
    version: ExecutableIdentity
    outputs: Json


def _environment(directory: str, executable: str) -> dict[str, str]:
    return {"HOME": directory, "TMPDIR": directory, "XDG_CACHE_HOME": directory,
            "PI_CODING_AGENT_DIR": directory, "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0", "NODE_DISABLE_COMPILE_CACHE": "1", "NO_COLOR": "1",
            "TERM": "dumb", "PATH": str(Path(executable).parent)}


def _probe_result(argv: tuple[str, ...], files: dict[str, str], result: ProcessResult,
                  timeout: float, output_limit: int) -> VersionProbe:
    status: Literal["ok", "timeout", "output_limit", "nonzero_exit"] = "ok"
    if result.reason == "evaluator_timeout":
        status = "timeout"
    elif result.reason == "evaluator_output_limit":
        status = "output_limit"
    elif result.returncode != 0:
        status = "nonzero_exit"
    return {"argv": list(argv), "admitted_files": files,
            "environment_policy": "minimal-environment/v1",
            "cwd_policy": "empty-temporary-directory/v1", "timeout_seconds": timeout,
            "output_limit_bytes": output_limit, "status": status, "returncode": result.returncode,
            "stdout_digest": _bytes_digest(result.stdout),
            "stderr_digest": _bytes_digest(result.stderr),
            "stdout_bytes": len(result.stdout), "stderr_bytes": len(result.stderr)}


def observe_executable(argv: tuple[str, ...], files: dict[str, str], *,
                       node: bool = False, timeout: float = 5,
                       output_limit: int = 8192) -> ExecutableObservation:
    """Observe only a caller-admitted version command, in an isolated temporary home."""
    if not argv or not Path(argv[0]).is_absolute() or not files:
        raise ValueError("version command requires absolute argv and admitted executable files")
    if not 0 < timeout <= 60 or not 1 <= output_limit <= 65536:
        raise ValueError("version probe limits exceed the VersionLock contract")
    for path, expected in files.items():
        if file_digest(Path(path)) != expected:
            raise ValueError("version command files changed after admission")
    with TemporaryDirectory(prefix="kernelcat-version-") as temporary:
        directory = str(Path(temporary).resolve())
        try:
            result = collect_process(argv, Path(directory), _environment(directory, argv[0]),
                                     b"", timeout, output_limit, output_limit)
        except OSError as error:
            probe = _probe_result(argv, files, ProcessResult(b"", b"", -1, None),
                                  timeout, output_limit)
            probe["status"] = "spawn_error"
            del probe["returncode"]
            return ExecutableObservation(
                {"identity": unknown(f"version command could not start: {type(error).__name__}"),
                 "probe": probe},
                {"argv": list(argv), "cwd": directory, "spawn_error": type(error).__name__})
        probe = _probe_result(argv, files, result, timeout, output_limit)
        outputs = {"argv": list(argv), "cwd": directory,
                   "stdout_base64": base64.b64encode(result.stdout).decode("ascii"),
                   "stderr_base64": base64.b64encode(result.stderr).decode("ascii")}
    return _parse_observation(probe, result.stdout, outputs, node)


def _parse_observation(probe: VersionProbe, stdout: bytes, outputs: Json,
                       node: bool) -> ExecutableObservation:
    reason = "version command " + probe["status"]
    if probe["status"] == "ok":
        try:
            value = stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            value = ""
        prefix = "v" if node else ""
        pattern = prefix + r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
        if not re.fullmatch(pattern, value):
            probe["status"] = "invalid_output"
            reason = "version command did not return one explicit semantic version"
        elif not _files_unchanged(probe["admitted_files"]):
            probe["status"] = "invalid_output"
            reason = "version command files changed during observation"
        else:
            identity = known(value, "executable", "sha256:" + digest(probe))
            return ExecutableObservation({"identity": identity, "probe": probe}, outputs)
    return ExecutableObservation({"identity": unknown(reason), "probe": probe}, outputs)


def _files_unchanged(files: dict[str, str]) -> bool:
    try:
        return all(file_digest(Path(path)) == expected for path, expected in files.items())
    except OSError:
        return False


def _git(executable: str, root: Path, arguments: tuple[str, ...]) -> str:
    argv = (executable, "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
            "-C", str(root), *arguments)
    with TemporaryDirectory(prefix="kernelcat-source-") as directory:
        environment = {"HOME": directory, "PATH": str(Path(executable).parent),
                       "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
        result = collect_process(argv, Path(directory), environment, b"", 5, 65536, 8192)
    if result.reason or result.returncode != 0:
        failure = result.reason or f"exit {result.returncode}"
        raise ValueError(f"source Git observation failed: {arguments[0]} ({failure})")
    return result.stdout.decode("utf-8").strip()


def _commit_permalink(remote: str, commit: str) -> str | None:
    if remote.startswith("git@") and ":" in remote:
        host, path = remote[4:].split(":", 1)
        remote = f"https://{host}/{path}"
    try:
        parsed = urlsplit(remote)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme not in {"https", "ssh"} or not parsed.hostname
            or parsed.query or parsed.fragment or parsed.password):
        return None
    if parsed.hostname != "github.com" or port not in {None, 22, 443}:
        return None
    path = parsed.path.removesuffix(".git").rstrip("/")
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        return None
    return f"https://{parsed.hostname}{path}/commit/{commit}"


def observe_source(anchor: Path, admitted_content_digest: str) -> SourceIdentity:
    """Describe the actual host source; HEAD never replaces the admitted byte digest."""
    executable = shutil.which("git")
    if executable is None:
        return unknown("source revision unavailable: Git executable not installed")
    try:
        root = Path(_git(executable, anchor.parent, ("rev-parse", "--show-toplevel")))
        _git(executable, root, ("ls-files", "--error-unmatch", "--",
                                str(anchor.resolve().relative_to(root))))
        commit = _git(executable, root, ("rev-parse", "--verify", "HEAD"))
        tree = _git(executable, root, ("rev-parse", "--verify", commit + "^{tree}"))
        status = _git(executable, root, ("status", "--porcelain=v1", "--untracked-files=normal"))
        source: KnownSource = {"state": "known", "commit": commit, "tree": tree,
            "worktree_state": "dirty" if status else "clean",
            "admitted_content_digest": admitted_content_digest}
        source["permalink_unavailable_reason"] = "commit has no locally observed origin ref"
        refs = _git(executable, root, ("for-each-ref", "--contains", commit,
                                       "--format=%(refname)", "refs/remotes/origin/"))
        if refs:
            remote = _git(executable, root, ("remote", "get-url", "origin"))
            permalink = _commit_permalink(remote, commit)
            if permalink is not None:
                del source["permalink_unavailable_reason"]
                source["commit_permalink"] = permalink
            else:
                source["permalink_unavailable_reason"] = "origin URL has no supported HTTPS permalink"
        current = _git(executable, root, ("rev-parse", "--verify", "HEAD"))
        if current != commit:
            return unknown("source HEAD changed during observation; no coherent revision retained")
        return source
    except UnicodeError:
        return unknown("source revision not observed: Git metadata is not valid UTF-8")
    except (OSError, ValueError) as error:
        detail = " ".join(str(error).split())[:1024]
        return unknown(f"source revision not observed: {type(error).__name__}: {detail}")
