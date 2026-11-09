"""Paths and hard constraints for demo1+demo2 32-card A/B and KTP jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
CAMPAIGN = TOOLS.parent
AB910 = TOOLS / "ab910.sh"
TEMPLATE = CAMPAIGN / "cann_submit" / "template"
LIVESRC = TOOLS / "livesrc910"
CANON_PATH = TOOLS / "op_canon.json"

DEMO1_HOST = "demo1@10.1.30.201"
DEMO1_PORT = 31222
DEMO1_BENCH = "/home/demo1/cann-bench-20260722"
DEMO2_HOST = "demo2@10.1.30.201"
DEMO2_PORT = 31222
DEMO2_BENCH = "/home/demo2/cann-bench-20260722"

SHARE_ROOT = "/models/share/ka"
SHARE_BENCH = f"{SHARE_ROOT}/cann-bench-20260722"
SHARE_CAMPAIGN = f"{SHARE_ROOT}/cann_campaign"
SHARE_RUNS = f"{SHARE_ROOT}/runs"
SHARE_JOBS = f"{SHARE_ROOT}/ktp_jobs"
SHARE_BIN = f"{SHARE_ROOT}/bin"
HOST_MODEL = "/mnt/host-model"

KTP_IMAGE = "10.1.30.201:31443/user-cdy-normal/sglang:v0.5.15-cann9.0.0-a3"
KTP_BIN = "ktp"
KTP_FRAMEWORK = "PyTorch"
KTP_JOB_TYPE = "acjob"
KTP_CPU = "16"
KTP_MEMORY = "64Gi"
KTP_NPU = 2
KTP_MAX_RUNTIME = 180
KTP_REPLICAS = 1

# demo1 logical devices to never use (dirty HBM / compile-fail slot).
SKIP_DEVS = (0, 12)
PREFER_DEVS = (10, 11, 13, 15)
# demo2 is a separate 16-NPU box; do not copy demo1's dirty-card skip.
DEMO2_SKIP_DEVS: tuple[int, ...] = ()
DEMO2_PREFER_DEVS = (10, 11, 13, 15)

FORBIDDEN_PREFIXES = (
    "/mnt/persistent",
    "/mnt/demo1-workspace",
    "/data_volume",
    "/home/demo1/workspace",
)

SSH_OPTS = (
    "-o", "BindInterface=en0",
    "-o", "BatchMode=yes",
    "-o", "UpdateHostKeys=no",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=4",
    "-o", "ConnectTimeout=20",
)


@dataclass(frozen=True)
class HostSpec:
    name: str
    ssh: str
    port: int = 31222
    bench: str = DEMO1_BENCH
    skip_devs: tuple[int, ...] = SKIP_DEVS
    prefer_devs: tuple[int, ...] = PREFER_DEVS
    logical_npu: int = 16
    max_ab910: int = 1

    @property
    def user(self) -> str:
        return self.ssh.split("@", 1)[0]

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
    hosts: dict[str, HostSpec] = field(default_factory=default_hosts)
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

    def host(self, name: str) -> HostSpec:
        if name not in self.hosts:
            raise KeyError(f"unknown host {name!r}; known {sorted(self.hosts)}")
        return self.hosts[name]
