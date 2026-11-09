"""Operator-owned file permissions shared by native Pi and legacy transports."""
from __future__ import annotations

from pathlib import Path

from kernelcat_executor.contracts.models import Action, Policy
from kernelcat_executor.operations.registry import GateDecision


def validate_paths(paths: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if paths is not None:
        for value in paths:
            path = Path(value)
            if (not value or value != path.as_posix() or path.is_absolute()
                    or set(path.parts) & {".", ".."} or "\\" in value):
                raise ValueError("workspace policy requires normalized relative paths")
    return paths


class WorkspaceGuard:
    name = "workspace-write-scope"

    def __init__(self, root: Path, writable: tuple[str, ...] | None,
                 readonly: tuple[str, ...] = ("reference",)) -> None:
        self.root = root.resolve()
        self.writable = validate_paths(writable)
        self.readonly = validate_paths(readonly) or ()

    def check(self, action: Action, scope: str, policy: Policy) -> GateDecision:
        if (action.tool not in {"write_file", "edit_file", "write", "edit"}
                or action.id.startswith(action.task + "/seed/")):
            return GateDecision(verdict="abstain")
        raw = action.arguments.get("path", "")
        path = Path(raw)
        try:
            name = (self.root / path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return GateDecision(verdict="deny", reason="workspace path escape")
        if (any(name == p or name.startswith(p + "/") for p in self.readonly)
                or (self.writable is not None and name not in self.writable)):
            return GateDecision(verdict="deny", reason="read-only task path")
        return GateDecision(verdict="abstain")
