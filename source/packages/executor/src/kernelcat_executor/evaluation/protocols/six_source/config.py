"""Explicit six-source inputs. Importing configuration performs no device probes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kernelcat_executor.build.inputs import BuildSettings

NUM_CORRECT_TRIALS = 5
NUM_PERF_TRIALS = 100
NUM_WARMUP = 3
SEED = 1024
BACKEND_ROOT = Path(__file__).resolve().parents[3] / "backends/ascend"


@dataclass(frozen=True)
class Request:
    candidate: Path
    reference: Path
    workspace: Path
    operator: str
    soc: str
    device: int = 0
    fast: bool = False
    timeout: int = 600

    @classmethod
    def from_dict(cls, data: dict, base: Path | None = None) -> Request:
        if not isinstance(data, dict) or set(data) - {
            "candidate", "reference", "workspace", "operator", "soc", "device", "fast", "timeout"
        }:
            raise ValueError("unknown evaluation request field")
        base = (base or Path.cwd()).resolve()
        paths = {}
        for key in ("candidate", "reference", "workspace"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} path is required")
            paths[key] = (base / value).resolve()
        request = cls(**{**data, **paths})
        BuildSettings(request.operator, request.workspace, request.soc, request.device)
        if type(request.fast) is not bool or type(request.timeout) is not int or not 1 <= request.timeout <= 86400:
            raise ValueError("fast must be boolean; timeout must be in [1,86400] seconds")
        for source in (request.candidate, request.reference):
            if not source.is_file() or not 0 < source.stat().st_size <= 4_000_000:
                raise ValueError("candidate and reference must be nonempty files at most 4 MB")
            if source.is_relative_to(request.workspace):
                raise ValueError("inputs must be outside the new job workspace")
        if request.workspace.is_relative_to(BACKEND_ROOT) or BACKEND_ROOT.is_relative_to(request.workspace):
            raise ValueError("job workspace must be disjoint from backend source")
        # A directory named source is not protection: use the actual workspace marker.
        checkout = next((p for p in BACKEND_ROOT.parents if (p / "workspace.json").is_file()), None)
        if checkout:
            for name in ("apps", "packages", "experiments", "contracts", "wiki"):
                protected = checkout / name
                if request.workspace.is_relative_to(protected) or protected.is_relative_to(request.workspace):
                    raise ValueError("job workspace must be disjoint from product and task evidence trees")
        return request

    def payload(self) -> dict:
        return {key: str(value) if isinstance(value, Path) else value for key, value in vars(self).items()}
