"""Controller-owned source workspace with atomic, optimistic file edits."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from kernelcat_executor.contracts.models import Action, Outcome, ToolFailure
from kernelcat_executor.contracts.text import text_page


class Workspace:
    """Not an OS sandbox. The task lease excludes other cooperative writers."""
    RESERVED = {".evaluations", ".kernelagent", ".git"}

    def __init__(self, root: Path, max_bytes: int = 262144, *, create: bool = True) -> None:
        self.root = root.resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def path(self, relative: str) -> Path:
        raw = Path(relative)
        if (not relative or not raw.parts or raw.is_absolute() or ".." in raw.parts
                or raw.parts[0] in self.RESERVED):
            raise ToolFailure("invalid or controller-reserved workspace path")
        current = self.root
        for part in raw.parts:
            current /= part
            if current.is_symlink():
                raise ToolFailure("workspace symlinks are forbidden")
        if not current.resolve().is_relative_to(self.root):
            raise ToolFailure("workspace path escape")
        return current

    def _read(self, path: Path) -> str:
        if not path.is_file() or path.stat().st_size > self.max_bytes:
            raise ToolFailure("file missing or too large")
        return path.read_bytes().decode("utf-8")

    def read(self, action: Action) -> Outcome:
        text = self._read(self.path(action.arguments["path"]))
        try:
            page = text_page(text, action.arguments.get("offset", 0), action.arguments.get("limit", 8192))
        except ValueError as error:
            raise ToolFailure(str(error)) from None
        return Outcome(data={"path": action.arguments["path"], **page})

    def write(self, action: Action) -> Outcome:
        path = self.path(action.arguments["path"])
        text = action.arguments["text"]
        if len(text.encode()) > self.max_bytes:
            raise ToolFailure("file too large")
        old = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if path.exists() and not path.is_file():
            raise ToolFailure("destination is not a regular file")
        if old != action.arguments.get("expected_sha256"):
            raise ToolFailure("optimistic file write conflict; read the current file first")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return Outcome(data={"path": action.arguments["path"],
                             "sha256": hashlib.sha256(text.encode()).hexdigest()})

    def edit(self, action: Action) -> Outcome:
        """One exact match plus old-file digest; no fuzzy or global replacement."""
        args = action.arguments
        text = self._read(self.path(args["path"]))
        old = args["old_text"]
        if not old or text.find(old) < 0 or text.find(old) != text.rfind(old):
            raise ToolFailure("edit requires one unique nonempty exact match")
        return self.write(Action(id=action.id, task=action.task, tool="write_file", arguments={
            "path": args["path"], "text": text.replace(old, args["new_text"], 1),
            "expected_sha256": args["expected_sha256"],
        }))
