"""Immutable local bytes; callers admit an owner intent before invoking writes."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.contracts.json_decoder import decode_object
from kernelcat_executor.contracts.models import Conflict, Json, ToolFailure, canonical
from kernelcat_executor.contracts.runtime import (
    PROGRAM_BYTES,
    RUNTIME_BYTES,
    bytes_digest,
    normalize_program,
)
from kernelcat_executor.generated.runtime import ProgramBundle


class RuntimeArtifacts:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def reference(self, raw: bytes, label: str) -> Json:
        checksum = bytes_digest(raw)
        identity = label + "/" + checksum.removeprefix("sha256:")
        return {"ref_id": identity, "owner": "program_store", "record_id": identity,
            "digest": checksum, "digest_scope": "exact UTF-8 " + label + " bytes",
            "locator": str(self.root / checksum.removeprefix("sha256:")), "byte_length": len(raw)}

    def put(self, raw: bytes, label: str) -> Json:
        limit = RUNTIME_BYTES if label == "knowledge-capture" else PROGRAM_BYTES
        if len(raw) > limit:
            raise Conflict("immutable " + label + " artifact exceeds its byte limit")
        reference = self.reference(raw, label)
        path = Path(reference["locator"])
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)
        self.read(reference)
        return reference

    def read(self, reference: Json) -> bytes:
        path = self.root / reference["digest"].removeprefix("sha256:")
        if reference["owner"] != "program_store" or reference["locator"] != str(path):
            raise Conflict("immutable reference does not belong to this ProgramStore")
        limit = RUNTIME_BYTES if reference["ref_id"].startswith("knowledge-capture/") else PROGRAM_BYTES
        if reference["byte_length"] > limit:
            raise Conflict("immutable artifact exceeds its byte limit")
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != reference["byte_length"]:
                raise Conflict("immutable artifact size/type differs from its owner reference")
            raw = stream.read(limit + 1)
        if len(raw) != reference["byte_length"] or bytes_digest(raw) != reference["digest"]:
            raise Conflict("immutable artifact bytes differ from the retained digest")
        return raw

    def program(self, reference: Json) -> ProgramBundle:
        raw = self.read(reference)
        normalized = normalize_program(decode_object(raw))
        if canonical(normalized).encode("utf-8") != raw:
            raise Conflict("immutable ProgramBundle is not its canonical retained bytes")
        return normalized


def _scan_error(error: OSError) -> None:
    raise ToolFailure("workspace snapshot scan failed at " + str(error.filename) + ": " + str(error)) from error


def workspace_bundle(workspace: Workspace, original: ProgramBundle, *,
                     source_overrides: list[Json] | None = None) -> ProgramBundle:
    files: list[Json] = []
    total = 0
    for directory, directories, names in os.walk(workspace.root, followlinks=False, onerror=_scan_error):
        for name in directories:
            path = Path(directory) / name
            if path.is_symlink() or name in Workspace.RESERVED:
                raise Conflict("workspace snapshot contains a symlink or controller-reserved directory")
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(workspace.root).as_posix()
            workspace.path(relative)
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise Conflict("workspace snapshot accepts regular UTF-8 source files only")
                raw = stream.read(PROGRAM_BYTES + 1)
            total += len(raw)
            if total > PROGRAM_BYTES or len(files) >= 2048:
                raise Conflict("workspace snapshot exceeds its source byte/file limit")
            files.append({"path": relative, "text": raw.decode("utf-8", errors="strict")})
    if source_overrides is not None:
        paths = {item["path"] for item in source_overrides}
        files = [*[item for item in files if item["path"] not in paths], *source_overrides]
    return normalize_program({**original, "files": files})
