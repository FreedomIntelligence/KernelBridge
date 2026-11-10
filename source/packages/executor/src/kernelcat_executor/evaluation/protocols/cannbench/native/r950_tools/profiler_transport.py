#!/usr/bin/env python3
"""Shared DevHost transport primitives for 910C profiler runners.

This module deliberately owns only packaging, SSH, locking, phase heartbeat,
and archive extraction.  It does not decide reward, promotion, or Wiki facts.
"""
from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent.parent
sys.path.insert(0, str(REPO))

from kernelcat_executor.backends.ascend.transport.direct_ssh import load_config  # noqa: E402
from kernelcat_executor.backends.ascend.transport.direct_ssh import ssh_exec  # noqa: E402

IGNORED_SOURCE_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
    }
)
_ACTIVE_REMOTE_LOCK: tuple[object, str, str] | None = None
_REMOTE_ELAPSED_RE = re.compile(r"(?:^|\s)elapsed_s=([0-9]+(?:\.[0-9]+)?)")


def _text(blob: bytes | None) -> str:
    return (blob or b"").decode("utf-8", "replace")


def _require_ack(output: bytes | None, marker: str, phase: str) -> None:
    """Require an exact acknowledgement line from a non-idempotent remote step."""
    if marker not in _text(output).splitlines():
        raise RuntimeError(f"remote {phase} did not report an explicit {marker}")


def _ignored_source_relative(relative: Path) -> bool:
    lowered = tuple(part.lower() for part in relative.parts)
    return (
        any(part in IGNORED_SOURCE_PARTS for part in lowered)
        or any(part.endswith(".egg-info") for part in lowered)
        or relative.suffix.lower() in {".pyc", ".pyo"}
    )


def _copytree_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        lowered = name.lower()
        if (
            lowered in IGNORED_SOURCE_PARTS
            or lowered.endswith(".egg-info")
            or Path(name).suffix.lower() in {".pyc", ".pyo"}
        ):
            ignored.add(name)
    return ignored


def _source_tree_sha256(root: Path) -> str:
    """Hash source paths and contents deterministically, excluding run artifacts."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"source tree is not a directory: {root}")
    digest = hashlib.sha256()
    digest.update(b"profiler-ab-source-tree/v1\0")
    files: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if _ignored_source_relative(relative) or not path.is_file():
            continue
        files.append((relative.as_posix(), path))
    for relative, path in sorted(files, key=lambda item: item[0]):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    return digest.hexdigest()


def _validate_package_inputs(
    template: Path, base_source: Path, candidate_source: Path
) -> None:
    required = {
        "template ops dispatcher": template / "csrc" / "ops" / "CMakeLists.txt",
        "base operator registration": base_source / "CMakeLists.txt",
        "candidate operator registration": candidate_source / "CMakeLists.txt",
    }
    missing = [f"{label}: {path}" for label, path in required.items() if not path.is_file()]
    if missing:
        raise ValueError("invalid profiler A/B package input; missing " + "; ".join(missing))


def _aicore_usages(text: str) -> dict[int, int]:
    return {
        int(device): int(usage)
        for device, usage in re.findall(
            r"\|\s*\d+\s+(\d+)\s+\|\s*[0-9A-Fa-f:.]+\s+\|\s*(\d+)\s+",
            text,
        )
    }


def _remote_run_path(run_id: str) -> str:
    """Build a /tmp child path from one non-traversable run-id component."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id or ""):
        raise ValueError(
            "--run-id must be one 1..128 character [A-Za-z0-9_.-] component"
        )
    remote_run = posixpath.normpath(f"/tmp/profab_{run_id}")
    if (
        posixpath.dirname(remote_run) != "/tmp"
        or not posixpath.basename(remote_run).startswith("profab_")
    ):
        raise ValueError(f"unsafe remote run path: {remote_run}")
    return remote_run


def _device_lock_path(device: int) -> str:
    if type(device) is not int or device < 0:
        raise ValueError("device lock requires a non-negative integer device id")
    return f"/tmp/profab_device_{device}.lock"


def _remote(
    command: str,
    cfg,
    *,
    timeout: int,
    stdin: bytes | None = None,
    display: bool = True,
) -> bytes:
    result = ssh_exec(command, cfg=cfg, timeout=timeout, stdin=stdin)
    stdout = _text(result.stdout)
    stderr = _text(result.stderr)
    if display and stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n", flush=True)
    if stderr and (display or result.returncode != 0):
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n", flush=True)
    campus_success = result.returncode in (255, -1, 0xFFFFFFFF) and bool(result.stdout)
    if result.returncode != 0 and not campus_success:
        raise RuntimeError(f"remote command failed rc={result.returncode}: {command[:200]}")
    return result.stdout or b""


def _acquire_remote_lock(cfg, *, device: int, owner: str) -> None:
    global _ACTIVE_REMOTE_LOCK
    if _ACTIVE_REMOTE_LOCK is not None:
        raise RuntimeError("a remote profiler lock is already active in this process")
    lock_path = _device_lock_path(device)
    script = f"""
set -eu
LOCK={shlex.quote(lock_path)}
if mkdir "$LOCK" 2>/dev/null; then
  printf '%s\n' {shlex.quote(owner)} > "$LOCK/owner"
  echo LOCK_OK
  exit 0
fi
echo "LOCK_BUSY path=$LOCK" >&2
test ! -f "$LOCK/owner" || sed -n '1p' "$LOCK/owner" >&2
exit 73
    """.strip()
    output = _remote("bash -lc " + shlex.quote(script), cfg, timeout=120)
    # If the fragile campus SSH drops after mkdir but before the marker is
    # observed, the owner-checked finally path should still attempt cleanup.
    _ACTIVE_REMOTE_LOCK = (cfg, lock_path, owner)
    _require_ack(output, "LOCK_OK", "device lock acquisition")


def _release_active_remote_lock() -> None:
    global _ACTIVE_REMOTE_LOCK
    active = _ACTIVE_REMOTE_LOCK
    _ACTIVE_REMOTE_LOCK = None
    if active is None:
        return
    cfg, lock_path, owner = active
    script = f"""
set -u
LOCK={shlex.quote(lock_path)}
OWNER={shlex.quote(owner)}
if test -f "$LOCK/owner" && test "$(sed -n '1p' "$LOCK/owner")" = "$OWNER"; then
  rm -f "$LOCK/owner"
  rmdir "$LOCK"
  echo LOCK_RELEASED
else
  echo "LOCK_NOT_OWNED path=$LOCK" >&2
  exit 74
fi
""".strip()
    try:
        output = _remote("bash -lc " + shlex.quote(script), cfg, timeout=120)
        _require_ack(output, "LOCK_RELEASED", "device lock release")
    except Exception as exc:  # best effort; never mask the run's real outcome
        print(f"WARNING remote profiler lock cleanup failed: {exc}", file=sys.stderr)


def _copy_source(
    template: Path, operator_source: Path, target: Path, *, snake: str = "maximum"
) -> None:
    shutil.copytree(template, target)
    ops = target / "csrc" / "ops"
    for child in ops.iterdir():
        if child.name == "CMakeLists.txt" and child.is_file():
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(
        operator_source,
        ops / snake,
        ignore=_copytree_ignore,
    )
    setup = target / "setup.py"
    text = setup.read_text(encoding="utf-8")
    anchor = "cpu_count = os.cpu_count() or 2"
    replacement = 'cpu_count = int(os.environ.get("CANN_BUILD_JOBS") or (os.cpu_count() or 2))'
    if text.count(anchor) == 1:
        setup.write_text(text.replace(anchor, replacement), encoding="utf-8")


def _package(base_source: Path, candidate_source: Path, *, snake: str = "maximum") -> bytes:
    template = REPO / "native" / "cann_submit" / "template"
    _validate_package_inputs(template, base_source, candidate_source)
    # TemporaryDirectory cleanup can recurse pathologically on the read-only
    # Windows template.  Keep this small staging tree for later OS cleanup; the
    # profiler payload and run are more important than synchronous teardown.
    stage = Path(tempfile.mkdtemp(prefix="profiler_ab_stage_"))
    _copy_source(template, base_source, stage / "base_source", snake=snake)
    _copy_source(template, candidate_source, stage / "mt_source", snake=snake)
    shutil.copy2(TOOLS / "pipeprof.py", stage / "pipeprof.py")
    shutil.copy2(
        TOOLS / "profiler_off_harness_patch.py",
        stage / "profiler_off_harness_patch.py",
    )
    handle = tempfile.NamedTemporaryFile(suffix=".tgz", delete=False)
    archive_path = Path(handle.name)
    handle.close()
    try:
        result = subprocess.run(
            ["tar", "-czf", str(archive_path), "-C", str(stage), "."],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "system tar failed rc=%d: %s"
                % (result.returncode, result.stderr[-1000:])
            )
        return archive_path.read_bytes()
    finally:
        archive_path.unlink(missing_ok=True)


def _phase(
    command: str,
    log_path: str,
    label: str,
    cfg,
    timeout: int = 1800,
) -> float | None:
    """Run one remote phase and return its self-reported wall time when present."""
    script = f"""
set -u
mkdir -p {shlex.quote(str(Path(log_path).parent).replace(os.sep, '/'))}
started_epoch=$(date +%s)
(
  {command}
) >{shlex.quote(log_path)} 2>&1 &
child=$!
while kill -0 "$child" 2>/dev/null; do
  echo "HEARTBEAT phase={label} pid=$child $(date -Is)"
  sleep 20
done
wait "$child"
rc=$?
finished_epoch=$(date +%s)
elapsed_s=$((finished_epoch-started_epoch))
echo "PHASE_DONE phase={label} rc=$rc elapsed_s=$elapsed_s $(date -Is)"
tail -120 {shlex.quote(log_path)} || true
exit "$rc"
""".strip()
    output = _remote("bash -lc " + shlex.quote(script), cfg, timeout=timeout)
    text = _text(output)
    marker = next(
        (
            line
            for line in reversed(text.splitlines())
            if f"PHASE_DONE phase={label} " in line
        ),
        "",
    )
    if f"PHASE_DONE phase={label} rc=0" not in marker:
        raise RuntimeError(f"phase {label} did not report an explicit rc=0")
    match = _REMOTE_ELAPSED_RE.search(marker)
    return float(match.group(1)) if match else None


def _safe_extract(
    payload: bytes,
    destination: Path,
    *,
    archive_path: Path | None = None,
    allow_existing: bool = False,
) -> None:
    if destination.exists():
        if not allow_existing:
            raise FileExistsError(destination)
        if not destination.is_dir():
            raise NotADirectoryError(destination)
    else:
        destination.mkdir(parents=True, exist_ok=False)
    if archive_path is None:
        archive_path = destination / "remote_artifacts.tgz"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(payload)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        root = destination.resolve()
        for member in archive.getmembers():
            if not (member.isdir() or member.isreg()):
                raise RuntimeError(
                    f"unsafe archive member type for {member.name}: {member.type!r}"
                )
            resolved = (destination / member.name).resolve()
            if root != resolved and root not in resolved.parents:
                raise RuntimeError(f"unsafe archive member: {member.name}")
        try:
            archive.extractall(destination, filter="data")
        except TypeError:  # Python < 3.12; types and paths were checked above.
            archive.extractall(destination)
