"""Narrow effect permissions to a host-verified LoopX Turn boundary."""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path, PurePosixPath

from kernelcat_executor.contracts.models import Action, Json, Policy, digest
from kernelcat_executor.operations.registry import GateDecision

READS = frozenset({"read_file", "read_receipt", "load_skill", "read_learned_skill", "finish",
                  "knowledge_search", "knowledge_show", "knowledge_source", "knowledge_read", "replay"})


@dataclass(frozen=True)
class LoopXTurnGuard:
    envelope: Json
    project: Path
    workspace: Path
    allow_evaluation: bool = False

    name: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", "loopx-turn-" + digest(self.envelope)[:20])

    def check(self, action: Action, scope: str, policy: Policy) -> GateDecision:
        boundary = self.envelope.get("boundary", {})
        # The replay tool records an intent; the later branch effect rechecks write scope.
        allowed = action.tool in READS
        if action.tool == "delegate":
            allowed = boundary.get("orchestration", {}).get("spawn_allowed") is True
        elif action.tool in {"evaluate", "run_experiment"}:
            allowed = self.allow_evaluation
        elif action.tool in {"write_file", "edit_file"}:
            path = action.arguments.get("path", "")
            scopes = boundary.get("write_scope", [])
            if isinstance(path, str) and isinstance(scopes, list):
                target = (self.workspace / path).resolve()
                if target.is_relative_to(self.workspace) and target.is_relative_to(self.project):
                    relative = PurePosixPath(target.relative_to(self.project).as_posix())
                    # Glob matches are rooted at the bound project, not any suffix.
                    allowed = any(isinstance(pattern, str) and not pattern.startswith("/")
                        and ".." not in PurePosixPath(pattern).parts
                        and (pattern == "**" or rooted_match(relative.as_posix(), pattern)) for pattern in scopes)
        return GateDecision(verdict="abstain" if allowed else "deny",
                            reason="verified_loopx_turn_boundary")


def rooted_match(path: str, pattern: str) -> bool:
    """Root-anchored glob with ** directories; * cannot cross a directory."""
    parts, glob = path.split("/"), pattern.split("/")
    @lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if j == len(glob):
            return i == len(parts)
        if glob[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i], glob[j]) and match(i + 1, j + 1)
    return match(0, 0)
