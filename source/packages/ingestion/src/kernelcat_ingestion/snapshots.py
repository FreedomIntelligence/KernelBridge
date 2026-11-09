"""Content-addressed derived objects, not a second source/evidence database."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from kernelcat_executor.contracts.models import Conflict, canonical


class DataError(ValueError):
    """Expected source acquisition, parsing or integrity refusal."""


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def hex_digest(ref: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", ref):
        raise DataError("invalid content identity")
    return ref[7:]


def install(path: Path, content: bytes) -> None:
    """Atomic create-if-absent; never replace a conflicting object or rival writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise Conflict("immutable object conflict") from None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


class Objects:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def put(self, value: Any) -> str:
        content = canonical(value).encode("utf-8")
        ref = sha256(content)
        install(self.root / hex_digest(ref), content)
        return ref

    def get(self, ref: str) -> Any:
        path = self.root / hex_digest(ref)
        if path.is_symlink():
            raise DataError("derived object may not be a symlink")
        content = path.read_bytes()
        if sha256(content) != ref:
            raise DataError("derived object digest mismatch")
        value = json.loads(content)
        if canonical(value).encode() != content:
            raise DataError("noncanonical derived object")
        return value
