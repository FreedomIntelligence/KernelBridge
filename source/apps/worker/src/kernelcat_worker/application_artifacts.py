"""Immutable source snapshots and evaluator identities for application transitions."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from kernelcat_executor.contracts.models import Conflict, canonical, digest
from kernelcat_executor.capabilities.workspace_policy import (
    RESERVED_WORKSPACE_PATHS as RESERVED_SOURCE_PATHS,
    canonical_workspace_path, filesystem_case_sensitive, workspace_path_key,
)

SOURCE_FILE_LIMIT = 262144
SOURCE_TREE_LIMIT = 16 * 1024 * 1024
SOURCE_COUNT_LIMIT = 4096
# JSON can escape each source/path character into six bytes; include bounded
# path metadata as well as contents so valid binary/text snapshots can reload.
SOURCE_JSON_LIMIT = 6 * (SOURCE_TREE_LIMIT + SOURCE_COUNT_LIMIT * 4096) + SOURCE_COUNT_LIMIT * 256


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
        if read_regular(path, len(encoded)) != encoded:
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


def source_path(name: str) -> Path:
    if (not isinstance(name, str) or not name or len(name) > 4096 or "\\" in name or "\0" in name
            or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))
            or name.split("/")[0] in RESERVED_SOURCE_PATHS):
        raise Conflict("source snapshot requires normalized, non-reserved relative paths")
    return Path(name)


def snapshot_contents(snapshot: dict, expected_sha256: str | None = None) -> dict[str, bytes]:
    """Validate both legacy text and binary checkpoints before using their identity."""
    files = snapshot.get("files") if isinstance(snapshot, dict) else None
    if (not isinstance(files, list) or len(files) > SOURCE_COUNT_LIMIT
            or digest(files) != snapshot.get("source_sha256")
            or expected_sha256 is not None and snapshot["source_sha256"] != expected_sha256):
        raise Conflict("source snapshot digest or file bound mismatch")
    contents, size = {}, 0
    for item in files:
        if not isinstance(item, dict) or set(item) not in (
                {"path", "text", "sha256", "mode"}, {"path", "base64", "sha256", "mode"}):
            raise Conflict("source snapshot requires exactly one content encoding")
        name = source_path(item["path"]).as_posix()
        if name in contents or type(item["mode"]) is not int or not 0 <= item["mode"] <= 0o777:
            raise Conflict("source snapshot contains duplicate paths or invalid modes")
        try:
            if "text" in item and isinstance(item["text"], str):
                content = item["text"].encode("utf-8")
            elif "base64" in item and isinstance(item["base64"], str):
                content = base64.b64decode(item["base64"], validate=True)
                if base64.b64encode(content).decode("ascii") != item["base64"]:
                    raise Conflict("source snapshot binary encoding is not canonical")
            else:
                raise Conflict("source snapshot content must be a string")
        except (UnicodeError, ValueError, binascii.Error) as error:
            raise Conflict("source snapshot content encoding is invalid") from error
        size += len(content)
        if len(content) > SOURCE_FILE_LIMIT or size > SOURCE_TREE_LIMIT:
            raise Conflict("source snapshot exceeds its bound")
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise Conflict("source file digest mismatch")
        contents[name] = content
    if any(parent.as_posix() in contents for name in contents for parent in Path(name).parents):
        raise Conflict("source snapshot contains conflicting file and directory paths")
    return contents


def read_source_snapshot(path: Path, expected_sha256: str | None = None) -> dict:
    snapshot = json.loads(read_regular(path, SOURCE_JSON_LIMIT))
    # Project-mode sessions intentionally retain conversation identity only.
    marker = {"source_sha256": digest({"snapshot_kind": "conversation_only"}),
              "snapshot_kind": "conversation_only", "files": []}
    if snapshot == marker and (expected_sha256 is None or expected_sha256 == marker["source_sha256"]):
        return snapshot
    snapshot_contents(snapshot, expected_sha256)
    return snapshot


def source_snapshot(root: Path, writable, readonly) -> dict:
    if root.resolve() != root or not root.is_dir():
        raise Conflict("source snapshot requires a canonical workspace directory")
    files = []
    size = 0
    case_sensitive = filesystem_case_sensitive(root)
    key = lambda name: workspace_path_key(name, case_sensitive)
    protected = [key(item) for item in readonly]
    reserved = {key(item) for item in RESERVED_SOURCE_PATHS}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        identity = key(name)
        if identity.split("/")[0] in reserved:
            continue
        if any(identity == item or identity.startswith(item + "/") for item in protected):
            continue
        if writable is not None and name not in writable:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if path.resolve() != path or not stat.S_ISREG(info.st_mode):
            raise Conflict("source snapshot refuses symlinks and special files")
        source_path(name)
        content = read_regular(path, SOURCE_FILE_LIMIT)
        size += len(content)
        if size > SOURCE_TREE_LIMIT or len(files) >= SOURCE_COUNT_LIMIT:
            raise Conflict("source snapshot exceeds its bound")
        try:
            encoded = {"text": content.decode("utf-8")}
        except UnicodeDecodeError:
            encoded = {"base64": base64.b64encode(content).decode("ascii")}
        files.append({"path": name, **encoded, "sha256": hashlib.sha256(content).hexdigest(), "mode": info.st_mode & 0o777})
    return {"source_sha256": digest(files), "files": files}


def restore_source(root: Path, snapshot: dict, writable, readonly) -> None:
    contents = snapshot_contents(snapshot)
    previous = source_snapshot(root, writable, readonly)
    desired = {item["path"]: item for item in snapshot["files"]}
    case_sensitive = filesystem_case_sensitive(root)
    key = lambda name: workspace_path_key(name, case_sensitive)
    protected = [key(path) for path in readonly]
    reserved = {key(path) for path in RESERVED_SOURCE_PATHS}
    names = {key(name) for name in desired}
    if len(names) != len(desired) or any(parent.as_posix() in names for name in names for parent in Path(name).parents):
        raise Conflict("source restore paths alias or overlap on this filesystem")
    obsolete = {item["path"] for item in previous["files"] if item["path"] not in desired}
    remove_directories = set()
    for name in desired:
        target = canonical_workspace_path(root, name)
        if not target.is_relative_to(root):
            raise Conflict("source restore path escapes the workspace")
        identity = key(name)
        if (identity.split("/")[0] in reserved or writable is not None and name not in writable or any(
                identity == path or identity.startswith(path + "/") or path.startswith(identity + "/") for path in protected)):
            raise Conflict("source restore attempts to change a protected file")
        # Preflight shape changes before touching anything. A directory can be
        # replaced only when every file below it is an authorized obsolete file.
        if target.is_dir():
            remove_directories.add(target)
            for child in target.rglob("*"):
                info = child.lstat()
                if stat.S_ISDIR(info.st_mode):
                    remove_directories.add(child)
                elif not stat.S_ISREG(info.st_mode) or child.relative_to(root).as_posix() not in obsolete:
                    raise Conflict("source restore directory contains protected or special files")
        for parent in target.parents:
            if parent == root:
                break
            if parent.is_file() and parent.relative_to(root).as_posix() not in obsolete:
                raise Conflict("source restore parent conflicts with a retained file")
    # The caller fenced admissions and writers. Remove only preflighted obsolete
    # files, then empty conflicting directories, then install desired files.
    # A prepared transition can repeat this sequence after an interrupted restore.
    for name in obsolete:
        (root / name).unlink()
    for directory in sorted(remove_directories, key=lambda path: len(path.parts), reverse=True):
        directory.rmdir()
    for name, item in desired.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".restore-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, item["mode"])
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(contents[name])
                stream.flush()
                os.fchmod(stream.fileno(), item["mode"])
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
