"""Immutable source snapshots and evaluator identities for application transitions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from kernelcat_executor.contracts.models import Conflict, canonical, digest


def read_regular(path: Path, maximum: int = 16 * 1024 * 1024) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise Conflict("artifact must be a bounded regular file")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise Conflict("artifact grew beyond its byte bound")
        return data


def persist(path: Path, value: dict) -> None:
    encoded = canonical(value).encode()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    except FileExistsError:
        if read_regular(path) != encoded:
            raise Conflict("immutable artifact identity collision")
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def evaluator_identity(config, task_path: Path) -> dict:
    paths = {task_path.resolve()}
    for tool in config.business_tools:
        paths.update(Path(item.path).resolve() for item in tool.files)
    if config.evaluator:
        paths.update(Path(item).resolve() for item in config.evaluator.argv if Path(item).is_file())
    if config.rsi:
        paths.update(Path(item["path"]).resolve() for item in config.rsi.get("adapter_files", []))
    files = {str(path): hashlib.sha256(read_regular(path, 256 * 1024 * 1024)).hexdigest() for path in sorted(paths)}
    return {"sha256": digest(files), "files": files}


def verify_evaluator(identity: dict) -> None:
    for path, expected in identity["files"].items():
        if hashlib.sha256(read_regular(Path(path), 256 * 1024 * 1024)).hexdigest() != expected:
            raise Conflict("frozen evaluator/task input changed: " + path)


def source_snapshot(root: Path, writable, readonly) -> dict:
    files = []
    size = 0
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if name.split("/")[0] in {".git", ".kernelagent", ".evaluations"}:
            continue
        if any(name == item or name.startswith(item + "/") for item in readonly):
            continue
        if writable is not None and name not in writable:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if path.resolve() != path or not stat.S_ISREG(info.st_mode):
            raise Conflict("source snapshot refuses symlinks and special files")
        content = read_regular(path, 262144)
        size += len(content)
        if size > 16 * 1024 * 1024 or len(files) >= 4096:
            raise Conflict("source snapshot exceeds its bound")
        files.append({"path": name, "text": content.decode("utf-8"), "sha256": hashlib.sha256(content).hexdigest(), "mode": info.st_mode & 0o777})
    return {"source_sha256": digest(files), "files": files}


def restore_source(root: Path, snapshot: dict, writable, readonly) -> None:
    if digest(snapshot["files"]) != snapshot["source_sha256"]:
        raise Conflict("source snapshot digest mismatch")
    previous = source_snapshot(root, writable, readonly)
    desired = {item["path"]: item for item in snapshot["files"]}
    for name, item in desired.items():
        target = root / name
        if not target.resolve().is_relative_to(root) or target.resolve() != target:
            raise Conflict("source restore path escapes the workspace")
        if writable is not None and name not in writable or any(name == path or name.startswith(path + "/") for path in readonly):
            raise Conflict("source restore attempts to change a protected file")
        if hashlib.sha256(item["text"].encode()).hexdigest() != item["sha256"]:
            raise Conflict("source file digest mismatch")
    # The caller has fenced admissions and confirmed no local writer remains.
    for name, item in desired.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".restore-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, item["mode"])
        with os.fdopen(fd, "w") as stream:
            stream.write(item["text"])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    for item in previous["files"]:
        if item["path"] not in desired:
            (root / item["path"]).unlink()
