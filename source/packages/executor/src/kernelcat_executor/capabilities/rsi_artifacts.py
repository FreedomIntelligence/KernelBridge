"""Finite RSI source transformations; admission and selection belong to callers."""
from __future__ import annotations

import hashlib

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.contracts.models import Action, Conflict, Json, canonical, digest


def source_files(workspace: Workspace, paths: list[str]) -> list[Json]:
    files = []
    for relative in sorted(paths):
        path = workspace.path(relative)
        if not path.is_file() or path.stat().st_size > workspace.max_bytes:
            raise Conflict("RSI source missing or too large: " + relative)
        files.append({"path": relative, "text": path.read_bytes().decode("utf-8")})
    if len(canonical(files).encode()) > 512 * 1024:
        raise Conflict("RSI source bundle exceeds 512 KiB")
    return files


def replace_source(workspace: Workspace, task_id: str, intent_id: str,
                   expected: list[Json], replacement: list[Json]) -> None:
    paths = [entry["path"] for entry in expected]
    if len(paths) != len(set(paths)) or len(replacement) != len({item["path"] for item in replacement}):
        raise Conflict("RSI source bundle repeats a path")
    if digest(source_files(workspace, paths)) != digest(expected):
        raise Conflict("RSI source changed after experiment submission")
    old = {entry["path"]: entry["text"] for entry in expected}
    if {entry["path"] for entry in replacement} != set(old):
        raise Conflict("RSI acceptance changed the protected source-file set")
    # One admitted finite effect owns the full operation. A crash after a partial
    # write stays UNKNOWN at the Gateway; it never authorizes replay or adoption.
    for entry in replacement:
        outcome = workspace.write(Action(id=intent_id + "/" + digest(entry["path"]),
            task=task_id, tool="write_file", arguments={**entry,
                "expected_sha256": hashlib.sha256(old[entry["path"]].encode()).hexdigest()}))
        if outcome.status != "completed":
            raise Conflict("RSI source write did not complete")
