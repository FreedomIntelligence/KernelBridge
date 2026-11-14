"""Portable A/B host and KTP settings. Remote targets require explicit configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
CAMPAIGN = TOOLS.parent
AB910 = TOOLS / "ab910.sh"
TEMPLATE = CAMPAIGN / "cann_submit" / "template"
LIVESRC = TOOLS / "livesrc910"
CANON_PATH = Path(os.environ.get("KB_AB_OPERATOR_CATALOG", str(TOOLS / "op_canon.json")))

# Keep the public demo1/demo2 backend names for compatibility; they are slots,
# not deployment addresses. Configure them through Config or these variables.
DEMO1_HOST = os.environ.get("KB_AB_HOST", os.environ.get("AB_SSH_HOST", ""))
if DEMO1_HOST and "@" not in DEMO1_HOST and os.environ.get("AB_SSH_USER"):
    DEMO1_HOST = f"{os.environ['AB_SSH_USER']}@{DEMO1_HOST}"
DEMO1_PORT = int(os.environ.get("KB_AB_PORT", os.environ.get("AB_SSH_PORT", "22")))
DEMO1_BENCH = os.environ.get("KB_AB_BENCH", os.environ.get("CANN_BENCH_HOME", ""))
DEMO2_HOST = os.environ.get("KB_AB_SECONDARY_HOST", "")
DEMO2_PORT = int(os.environ.get("KB_AB_SECONDARY_PORT", "22"))
DEMO2_BENCH = os.environ.get("KB_AB_SECONDARY_BENCH", "")

SHARE_ROOT = os.environ.get("KB_AB_SHARE_ROOT", "/models/share/kernelbridge")
SHARE_BENCH = os.environ.get("KB_AB_SHARE_BENCH", f"{SHARE_ROOT}/cann-bench")
SHARE_CAMPAIGN = f"{SHARE_ROOT}/cann_campaign"
SHARE_RUNS = f"{SHARE_ROOT}/runs"
SHARE_JOBS = f"{SHARE_ROOT}/ktp_jobs"
SHARE_BIN = f"{SHARE_ROOT}/bin"
HOST_MODEL = os.environ.get("KB_AB_HOST_MODEL", "/mnt/host-model")

KTP_IMAGE = os.environ.get("KB_KTP_IMAGE", "")
KTP_BIN = "ktp"
KTP_FRAMEWORK = "PyTorch"
KTP_JOB_TYPE = "acjob"
KTP_CPU = "16"
KTP_MEMORY = "64Gi"
KTP_NPU = 2
KTP_MAX_RUNTIME = 180
KTP_REPLICAS = 1


def _devices(name: str, default: str = "") -> tuple[int, ...]:
    return tuple(int(value) for value in os.environ.get(name, default).replace(",", " ").split())


SKIP_DEVS = _devices("KB_AB_SKIP_DEVS")
PREFER_DEVS = _devices("KB_AB_DEVS", "0")
DEMO2_SKIP_DEVS = _devices("KB_AB_SECONDARY_SKIP_DEVS")
DEMO2_PREFER_DEVS = _devices("KB_AB_SECONDARY_DEVS", "0")
FORBIDDEN_PREFIXES = tuple(filter(None, os.environ.get("KB_AB_FORBIDDEN_PATHS", "").split(os.pathsep)))

SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=4",
    "-o", "ConnectTimeout=20",
)


@dataclass(frozen=True)
class HostSpec:
    name: str
    ssh: str
    port: int = 22
    bench: str = DEMO1_BENCH
    skip_devs: tuple[int, ...] = SKIP_DEVS
    prefer_devs: tuple[int, ...] = PREFER_DEVS
    logical_npu: int = 1
    max_ab910: int = 1

    @property
    def user(self) -> str:
        return self.ssh.split("@", 1)[0] if "@" in self.ssh else ""

    @property
    def hostname(self) -> str:
        return self.ssh.split("@", 1)[-1]


def default_hosts() -> dict[str, HostSpec]:
    return {
        "demo1": HostSpec(
            name="demo1",
            ssh=DEMO1_HOST,
            port=DEMO1_PORT,
            bench=DEMO1_BENCH,
            skip_devs=SKIP_DEVS,
            prefer_devs=PREFER_DEVS,
        ),
        "demo2": HostSpec(
            name="demo2",
            ssh=DEMO2_HOST,
            port=DEMO2_PORT,
            bench=DEMO2_BENCH,
            skip_devs=DEMO2_SKIP_DEVS,
            prefer_devs=DEMO2_PREFER_DEVS,
        ),
    }


@dataclass
class Config:
    demo1_host: str = DEMO1_HOST
    demo1_port: int = DEMO1_PORT
    demo1_bench: str = DEMO1_BENCH
    demo2_host: str = DEMO2_HOST
    demo2_port: int = DEMO2_PORT
    demo2_bench: str = DEMO2_BENCH
    hosts: dict[str, HostSpec] = field(default_factory=dict)
    share_root: str = SHARE_ROOT
    share_bench: str = SHARE_BENCH
    share_campaign: str = SHARE_CAMPAIGN
    share_runs: str = SHARE_RUNS
    share_jobs: str = SHARE_JOBS
    ktp_image: str = KTP_IMAGE
    ktp_cpu: str = KTP_CPU
    ktp_memory: str = KTP_MEMORY
    ktp_npu: int = KTP_NPU
    ktp_max_runtime: int = KTP_MAX_RUNTIME
    skip_devs: tuple[int, ...] = SKIP_DEVS
    prefer_devs: tuple[int, ...] = PREFER_DEVS
    forbidden: tuple[str, ...] = FORBIDDEN_PREFIXES
    extra_env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.hosts:
            self.hosts = {
                "demo1": HostSpec("demo1", self.demo1_host, self.demo1_port,
                                  self.demo1_bench, self.skip_devs, self.prefer_devs,
                                  logical_npu=int(os.environ.get("KB_AB_NPU_COUNT", "1"))),
                "demo2": HostSpec("demo2", self.demo2_host, self.demo2_port,
                                  self.demo2_bench, DEMO2_SKIP_DEVS, DEMO2_PREFER_DEVS,
                                  logical_npu=int(os.environ.get("KB_AB_SECONDARY_NPU_COUNT", "1"))),
            }

    def host(self, name: str) -> HostSpec:
        if name not in self.hosts:
            raise KeyError(f"unknown host {name!r}; known {sorted(self.hosts)}")
        return self.hosts[name]
