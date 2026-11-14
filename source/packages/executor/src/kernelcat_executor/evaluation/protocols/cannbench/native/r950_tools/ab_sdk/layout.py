"""Configured host geometry and KTP job fields."""
from __future__ import annotations

from .config import Config, KTP_IMAGE, KTP_CPU, KTP_MEMORY, KTP_NPU, KTP_MAX_RUNTIME

DEMO1_LOGICAL_NPU = 1
DEMO1_MAX_AB910 = 1  # kernel_eval gate: never two ab910 on the SAME host
# cmake is isolated per Job (/tmp private src+scripts, tasks symlink). Concurrent
# Running KTP Jobs are cmake-safe. Default plan() stays sequential so a lone
# `plan -n N` cannot dump the queue; pass parallel= or fill_fleet().
KTP_SUBMIT_PARALLEL = 1
# Safe submit-wave size once cmake isolation is on: 8 Jobs × npu=2 = 16 exclusive cards.
KTP_SAFE_PARALLEL = 8
KTP_CMAKE_ISOLATED = True


def _host_layout(spec) -> dict:
    configured = bool(spec.ssh)
    devices = list(range(spec.logical_npu)) if configured else []
    skip = list(spec.skip_devs)
    usable = [d for d in devices if d not in skip]
    prefer = [d for d in spec.prefer_devs if d in usable][:2]
    if len(prefer) < 2:
        prefer = usable[:2]
    return {
        "name": spec.name,
        "ssh": spec.ssh,
        "port": spec.port,
        "bench": spec.bench,
        "logical_npu": spec.logical_npu if configured else 0,
        "devices": devices,
        "skip": skip,
        "usable": usable,
        "prefer": prefer,
        "ab_conc": len(prefer),
        "max_ab910": spec.max_ab910 if configured else 0,
    }


def layout(cfg: Config | None = None) -> dict:
    cfg = cfg or Config()
    hosts = {name: _host_layout(spec) for name, spec in cfg.hosts.items()}
    d1 = hosts["demo1"]
    total_npu = sum(h["logical_npu"] for h in hosts.values())
    max_ab = sum(h["max_ab910"] for h in hosts.values())
    return {
        "demo1": d1,
        "demo2": hosts.get("demo2") or {},
        "hosts": hosts,
        "total_logical_npu": total_npu,
        "max_concurrent_ab910": max_ab,
        "ktp": {
            "npu_per_job": cfg.ktp_npu or KTP_NPU,
            "cpu": cfg.ktp_cpu or KTP_CPU,
            "memory": cfg.ktp_memory or KTP_MEMORY,
            "max_runtime": cfg.ktp_max_runtime or KTP_MAX_RUNTIME,
            "ab_devs": [0, 1],
            "ab_conc": 2,
            "submit_parallel": KTP_SUBMIT_PARALLEL,
            "safe_parallel": KTP_SAFE_PARALLEL,
            "cmake_isolated": KTP_CMAKE_ISOLATED,
            "image": cfg.ktp_image or KTP_IMAGE,
            "job_type": "acjob",
            "framework": "PyTorch",
        },
    }


def fill_ktp_job(
    n_ops: int,
    cfg: Config | None = None,
    *,
    parallel: int | None = None,
) -> dict:
    """Agent asks for N operators; SDK fills every Job field.

    `parallel` is submit-wave size (Jobs in one SSH bundle). Default 1 keeps
    `plan -n N` conservative. cmake isolation means Running Jobs may overlap.
    """
    if n_ops < 1:
        raise ValueError("n_ops >= 1")
    lay = layout(cfg)
    k = lay["ktp"]
    job = {
        "npu": k["npu_per_job"],
        "cpu": k["cpu"],
        "memory": k["memory"],
        "max_runtime": k["max_runtime"],
        "ab_devs": " ".join(str(d) for d in k["ab_devs"]),
        "ab_conc": k["ab_conc"],
        "image": k["image"],
        "job_type": k["job_type"],
        "framework": k["framework"],
    }
    wave = k["submit_parallel"] if parallel is None else int(parallel)
    if wave < 1:
        raise ValueError("parallel >= 1")
    inflight = min(n_ops, wave)
    return {
        "backend": "ktp",
        "n_ops": n_ops,
        "submit_parallel": inflight,
        "npu_inflight": inflight * k["npu_per_job"],
        "npu_total": n_ops * k["npu_per_job"],
        "cmake_isolated": k.get("cmake_isolated", True),
        "jobs": [dict(job) for _ in range(n_ops)],
    }


def fill_fleet(
    n_ops: int,
    cfg: Config | None = None,
    *,
    ktp_parallel: int | None = None,
) -> dict:
    """32-card hosts (1 ab910 each) + remaining ops on KTP npu=2.

    demo1/demo2 kernel_eval forbids a second ab910 on the *same* host. Extra
    operators go to KTP. cmake is isolated, so KTP Jobs may run concurrently.
    """
    if n_ops < 1:
        raise ValueError("n_ops >= 1")
    lay = layout(cfg)
    host_names = [name for name, host in lay["hosts"].items()
                  if host["max_ab910"] > 0 and host["ab_conc"] > 0]
    host_slots = min(len(host_names), n_ops)
    hosts = [fill_host_ab(host_names[i], cfg) for i in range(host_slots)]
    ktp_ops = n_ops - host_slots
    if ktp_ops and not lay["ktp"]["image"]:
        raise ValueError("Insufficient configured host slots; configure KB_KTP_IMAGE for remaining operators")
    wave = KTP_SAFE_PARALLEL if ktp_parallel is None else int(ktp_parallel)
    ktp = fill_ktp_job(ktp_ops, cfg, parallel=wave) if ktp_ops else None
    return {
        "backend": "fleet",
        "n_ops": n_ops,
        "total_logical_npu": lay["total_logical_npu"],
        "max_concurrent_ab910": lay["max_concurrent_ab910"],
        "host_slots": host_slots,
        "hosts": hosts,
        "ktp_jobs": ktp_ops,
        "ktp": ktp,
        "note": (
            "1 ab910 per 16-card host (kernel_eval); remaining operators are "
            "KTP Jobs npu=2 with isolated /tmp cmake. Do not yaml-batch-submit."
        ),
    }


def fill_host_ab(name: str = "demo1", cfg: Config | None = None) -> dict:
    lay = layout(cfg)
    d = (lay.get("hosts") or {}).get(name) or lay["demo1"]
    return {
        "backend": name,
        "n_ops": d["max_ab910"],
        "devices": d["prefer"],
        "ab_conc": d["ab_conc"],
        "skip": d["skip"],
        "logical_npu": d["logical_npu"],
        "npu_total": len(d["prefer"]),
        "ssh": d.get("ssh"),
        "bench": d.get("bench"),
    }


def fill_demo1_ab(cfg: Config | None = None) -> dict:
    return fill_host_ab("demo1", cfg)


def fill_demo2_ab(cfg: Config | None = None) -> dict:
    return fill_host_ab("demo2", cfg)


def parse_queue_used(text: str) -> tuple[int | None, int | None]:
    """Parse `ktp queues` USED/DESERVED. Returns (used, deserved)."""
    for line in text.splitlines():
        if "normal-queue" not in line:
            continue
        parts = line.split()
        # NAME TYPE USED / DESERVED STATUS ...
        for i, p in enumerate(parts):
            if p == "/" and i >= 1 and i + 1 < len(parts):
                try:
                    return int(parts[i - 1]), int(parts[i + 1])
                except ValueError:
                    return None, None
    return None, None
