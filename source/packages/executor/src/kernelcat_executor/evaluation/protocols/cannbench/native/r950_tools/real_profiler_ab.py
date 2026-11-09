#!/usr/bin/env python3
"""Compatibility entry point for the Stage-1 profiler runner.

The execution implementation lives in :mod:`profiler_run`.  Private transport
aliases remain temporarily available because ``real_profiler_multi_event.py``
and legacy regression tests still import this module during the adapter
migration.  Compatibility wrappers preserve module-level monkeypatching.
"""
from __future__ import annotations

import copy
import io
import tarfile
from pathlib import Path
from typing import Any

import profiler_transport as _transport
from profiler_feedback_sdk import query_completed_run, serialize_feedback
from profiler_run import main


REPO = _transport.REPO
TOOLS = _transport.TOOLS
_aicore_usages = _transport._aicore_usages
_copy_source = _transport._copy_source
_device_lock_path = _transport._device_lock_path
_phase = _transport._phase
_remote = _transport._remote
_remote_run_path = _transport._remote_run_path
_require_ack = _transport._require_ack
_safe_extract = _transport._safe_extract
_source_tree_sha256 = _transport._source_tree_sha256
_text = _transport._text
_validate_package_inputs = _transport._validate_package_inputs
load_config = _transport.load_config
_ORIGINAL_PACKAGE = getattr(
    _transport,
    "_kernelcat_original_package",
    _transport._package,
)
_transport._kernelcat_original_package = _ORIGINAL_PACKAGE


def _normalize_package_members(payload: bytes) -> bytes:
    """Remove tar's synthetic ``./`` root without changing staged contents."""
    source_stream = io.BytesIO(payload)
    output_stream = io.BytesIO()
    with tarfile.open(fileobj=source_stream, mode="r:gz") as source:
        with tarfile.open(fileobj=output_stream, mode="w:gz") as output:
            for member in source.getmembers():
                normalized = member.name
                while normalized.startswith("./"):
                    normalized = normalized[2:]
                if normalized in {"", "."}:
                    continue
                copied = copy.copy(member)
                copied.name = normalized
                if copied.linkname:
                    linkname = copied.linkname
                    while linkname.startswith("./"):
                        linkname = linkname[2:]
                    copied.linkname = linkname
                fileobj = source.extractfile(member) if member.isfile() else None
                output.addfile(copied, fileobj)
    return output_stream.getvalue()


def _package(
    base_source: Path,
    candidate_source: Path,
    *,
    snake: str = "maximum",
) -> bytes:
    """Build the legacy payload with canonical top-level member names."""
    return _normalize_package_members(
        _ORIGINAL_PACKAGE(base_source, candidate_source, snake=snake)
    )


# ``profiler_run`` resolves ``transport._package`` at call time.  Installing
# the canonical wrapper here keeps the legacy CLI and direct compatibility
# imports on the same archive contract while the transport module is migrated.
_transport._package = _package


def _call_with_compat_remote(function, /, *args: Any, **kwargs: Any) -> Any:
    """Run a transport helper through the patchable legacy ``_remote`` alias."""
    original = _transport._remote
    _transport._remote = _remote
    try:
        return function(*args, **kwargs)
    finally:
        _transport._remote = original


def _acquire_remote_lock(cfg, *, device: int, owner: str) -> None:
    _call_with_compat_remote(
        _transport._acquire_remote_lock,
        cfg,
        device=device,
        owner=owner,
    )


def _release_active_remote_lock() -> None:
    _call_with_compat_remote(_transport._release_active_remote_lock)


def _write_unified_feedback(
    artifact_dir: Path, *, action_cell: str
) -> tuple[Path, dict[str, Any]]:
    """Legacy one-shot reducer with atomic publication.

    New executions use :func:`profiler_run._reduce_feedback`; this façade keeps
    the former test/integration surface while calling the same lossless SDK.
    """
    artifact_dir = Path(artifact_dir)
    output = artifact_dir / "unified_feedback.json"
    temporary = artifact_dir / ".unified_feedback.json.tmp"
    if output.exists() or temporary.exists():
        raise FileExistsError(f"refusing existing unified feedback output: {output}")
    payload = query_completed_run([artifact_dir], action_cell=action_cell)
    try:
        temporary.write_text(serialize_feedback(payload), encoding="utf-8")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output, payload


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
