"""Explicit native build inputs; importing them performs no probe."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuildSettings:
    operator: str
    workspace: Path
    soc: str
    device: int = 0

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.operator):
            raise ValueError("operator must be a single identifier")
        if not re.fullmatch(r"ai_core-Ascend[A-Za-z0-9_]+", self.soc):
            raise ValueError("supply the exact installed target as ai_core-Ascend...")
        if type(self.device) is not int or self.device < 0:
            raise ValueError("device must be a nonnegative integer")
        if not self.workspace.is_absolute():
            raise ValueError("workspace must be absolute")
