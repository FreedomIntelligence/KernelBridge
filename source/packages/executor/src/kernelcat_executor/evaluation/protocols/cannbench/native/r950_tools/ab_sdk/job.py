"""A/B job spec, launch handle, and path / NPU guards."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .config import CANON_PATH, Config, KTP_NPU, LIVESRC

_CANON: dict[str, str] | None = None
_ALIASES = {
    "egfv2": "engramgatefusion",
    "gatherv2": "gather",
    "lstmhidkeep": "lstm",
    "maximumv2": "maximum",
    "mfrv2": "moefinalizerouting",
    "mhcv2": "mhcsinkhorn",
    "scatterv2": "scatter",
}


def load_canon() -> dict[str, str]:
    global _CANON
    if _CANON is None:
        _CANON = json.loads(CANON_PATH.read_text())
    return _CANON


def canon_op(name: str) -> str:
    """Map snake / dir / messy name to official CamelCase."""
    raw = name.strip()
    if not raw:
        raise ValueError("empty operator name")
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    key = _ALIASES.get(key, key)
    canon = load_canon()
    if key in canon:
        return canon[key]
    if re.fullmatch(r"[A-Z][A-Za-z0-9]+", raw):
        return raw
    raise KeyError(f"unknown operator {name!r}; add it to op_canon.json")


def livesrc_dir(op: str) -> Path:
    camel = canon_op(op)
    for d in LIVESRC.iterdir():
        if not d.is_dir():
            continue
        try:
            if canon_op(d.name) == camel:
                return d
        except KeyError:
            continue
    raise FileNotFoundError(f"no livesrc910 dir for {camel}")


def guard_path(path: str | Path, cfg: Config | None = None) -> str:
    # Remote demo/KTP paths are POSIX. pathlib.Path("/mnt/foo") on Windows
    # becomes "\mnt\foo" and would skip the forbidden-prefix check.
    raw = os.path.expanduser(str(path))
    posix = raw.replace("\\", "/")
    forbidden = (cfg.forbidden if cfg is not None else Config().forbidden)
    for prefix in forbidden:
        pref = str(prefix).replace("\\", "/").rstrip("/")
        if posix == pref or posix.startswith(pref + "/"):
            raise ValueError(
                f"path {posix} is on exclusive/RWO storage; put code on "
                f"/models/share or /mnt/host-model"
            )
    if posix.startswith("/") and not posix.startswith("//"):
        return posix
    return raw


def guard_npu(npu: int, *, allow_wide: bool = False) -> int:
    if npu % 2 != 0 or npu < 2:
        raise ValueError(
            f"KTP --npu must be even and >= 2 (got {npu}); "
            "2 = one exclusive card"
        )
    if npu > 2 and not allow_wide:
        raise ValueError(
            f"KTP A/B is one operator per job at npu={KTP_NPU}; "
            f"refusing npu={npu}. Pass allow_wide=True only if you mean it"
        )
    return npu


@dataclass
class AbSpec:
    op: str
    reps: int
    arms: dict[str, str]
    tag: str = ""
    cann: str = "9"
    build_jobs: int = 16
    devices: tuple[int, ...] | None = None
    npu: int = KTP_NPU
    max_runtime: int = 180
    # Optional override of CANN_BENCH_HOME for probe task sets (e.g. hidden-signature
    # replicas).  Empty string = the platform default bench (cfg.share_bench).  The
    # override path must live on the share disk like every other job artifact.
    bench: str = ""

    def __post_init__(self) -> None:
        self.op = canon_op(self.op)
        if self.reps < 1:
            raise ValueError("reps must be >= 1")
        if len(self.arms) < 1:
            raise ValueError("need at least one arm")
        if not self.tag:
            self.tag = f"{self.op.lower()}-{'-'.join(self.arms)}"[:48]

    def arm_specs(self) -> list[str]:
        out = []
        for name, src in self.arms.items():
            src_s = str(Path(src).expanduser().resolve())
            if not (Path(src_s) / "op_kernel").is_dir():
                raise FileNotFoundError(f"{name}: {src_s} has no op_kernel/")
            out.append(f"{name}:{src_s}")
        return out

    @classmethod
    def from_mapping(cls, raw: Mapping) -> "AbSpec":
        arms = raw.get("arms") or {}
        if isinstance(arms, list):
            arms = {a["name"]: a["src"] for a in arms}
        return cls(
            op=str(raw["op"]),
            reps=int(raw.get("reps", 2)),
            arms=dict(arms),
            tag=str(raw.get("tag", "")),
            cann=str(raw.get("cann", "9")),
            build_jobs=int(raw.get("build_jobs", 16)),
            npu=int(raw.get("npu", KTP_NPU)),
            max_runtime=int(raw.get("max_runtime", 180)),
            bench=str(raw.get("bench", "")),
        )


@dataclass
class AbLaunch:
    backend: str
    spec: AbSpec
    job_id: str = ""
    log_path: str = ""
    yaml_path: str = ""
    command: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "op": self.spec.op,
            "tag": self.spec.tag,
            "job_id": self.job_id,
            "log_path": self.log_path,
            "yaml_path": self.yaml_path,
            "extra": self.extra,
        }
