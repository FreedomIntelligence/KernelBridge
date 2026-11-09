"""Small finite native ports; no SDK, device or protocol selection on import."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from kernelcat_executor.build.inputs import BuildSettings
from kernelcat_executor.build.outputs import BuildOutputs


class Compiler(Protocol):
    def __call__(self, sources: Mapping[str, str], settings: BuildSettings,
                 extra_kernel_include_paths: tuple[str, ...] = ()) -> BuildOutputs: ...


class Runtime(Protocol):
    def load(self, outputs: BuildOutputs, model_source: str) -> None: ...
    def close(self) -> None: ...
