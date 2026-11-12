"""Operator-owned file permissions shared by native Pi and legacy transports."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import unicodedata

from kernelcat_executor.contracts.models import Action, Conflict, Policy
from kernelcat_executor.operations.registry import GateDecision

RESERVED_WORKSPACE_PATHS = {".git", ".kernelagent", ".evaluations"}


def filesystem_case_sensitive(root: Path) -> bool:
    # Darwin's pathconf reports the actual volume capability, including
    # case-sensitive APFS. Numeric 11 is _PC_CASE_SENSITIVE in sys/unistd.h.
    if sys.platform == "darwin":
        value = os.pathconf(root, 11)
        if value not in (0, 1):
            raise Conflict("workspace filesystem case sensitivity is unavailable")
        return bool(value)
    return True


def workspace_path_key(name: str, case_sensitive: bool) -> str:
    return name if case_sensitive else unicodedata.normalize("NFD", name).upper().lower()


def canonical_workspace_path(root: Path, name: str) -> Path:
    """Reject filesystem aliases and symlinks, including aliased ancestors."""
    current = root
    for part in Path(name).parts:
        try:
            names = os.listdir(current)
        except (FileNotFoundError, NotADirectoryError):
            break
        target = current / part
        try:
            info = target.lstat()
        except FileNotFoundError:
            break
        if part not in names:
            raise Conflict("workspace path uses a non-canonical filesystem alias")
        if stat.S_ISLNK(info.st_mode):
            raise Conflict("workspace symlinks are forbidden")
        current = target
    return root / name


def validate_paths(paths: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if paths is not None:
        for value in paths:
            path = Path(value)
            if (not value or value == "." or value != path.as_posix() or path.is_absolute()
                    or set(path.parts) & {".", ".."} or "\\" in value or "\0" in value):
                raise ValueError("workspace policy requires normalized relative paths")
    return paths


class WorkspaceGuard:
    name = "workspace-write-scope"

    def __init__(self, root: Path, writable: tuple[str, ...] | None,
                 readonly: tuple[str, ...] = ("reference",)) -> None:
        self.root = root.resolve()
        self.writable = validate_paths(writable)
        self.readonly = validate_paths(readonly) or ()
        self.case_sensitive = filesystem_case_sensitive(self.root)

    def check(self, action: Action, scope: str, policy: Policy) -> GateDecision:
        if (action.tool not in {"write_file", "edit_file", "write", "edit"}
                or action.id.startswith(action.task + "/seed/")):
            return GateDecision(verdict="abstain")
        raw = action.arguments.get("path", "")
        path = Path(raw)
        try:
            name = Path(os.path.abspath(self.root / path)).relative_to(self.root).as_posix()
            canonical_workspace_path(self.root, name)
        except (ValueError, OSError, Conflict):
            return GateDecision(verdict="deny", reason="workspace path escape")
        key = workspace_path_key(name, self.case_sensitive)
        protected = [workspace_path_key(p, self.case_sensitive) for p in self.readonly]
        reserved = {workspace_path_key(p, self.case_sensitive) for p in RESERVED_WORKSPACE_PATHS}
        if (name == "." or key.split("/")[0] in reserved
                or any(key == p or key.startswith(p + "/") or p.startswith(key + "/") for p in protected)
                or (self.writable is not None and name not in self.writable)):
            return GateDecision(verdict="deny", reason="read-only task path")
        return GateDecision(verdict="abstain")
