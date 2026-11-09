"""Compatibility raw-source wrapper over the shared filesystem adapter."""
from __future__ import annotations

from pathlib import Path

from kernelcat_knowledge.api.mcp import config
from kernelcat_knowledge.api.mcp.index_loader import IndexLoader
from kernelcat_knowledge.generated.python_startup import (
    ExpectedPythonModules,
    admit_modules,
)
from kernelcat_knowledge.storage.filesystem import RawReader as _CoreRawReader


class RawReader(_CoreRawReader):
    def __init__(
        self,
        raw_root: Path | None = None,
        loader: IndexLoader | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
        *,
        expected_modules: ExpectedPythonModules,
    ) -> None:
        admit_modules(expected_modules, ("kernelcat_knowledge",))
        root = (raw_root or config.CONFIG.raw_root or (config.CONFIG.wiki_root.parent / "raw")).resolve()
        super().__init__(
            raw_root=root,
            repo_root=root.parent,
            loader=loader if loader is not None else IndexLoader(expected_modules=expected_modules),
            max_lines=int(max_lines or config.CONFIG.raw_max_lines),
            max_bytes=int(max_bytes or config.CONFIG.raw_max_bytes),
        )


__all__ = ["RawReader"]
