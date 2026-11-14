"""Facade for configured-host A/B and KTP jobs."""
from __future__ import annotations

import os

from .config import Config
from .demo1 import Demo1Backend, Demo2Backend, HostBackend
from .job import AbLaunch, AbSpec
from .ktp import KtpBackend
from .layout import fill_demo1_ab, fill_fleet, fill_host_ab, fill_ktp_job, layout, parse_queue_used
from .share import Share
from . import cases as cases_mod


class AbClient:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.demo1 = Demo1Backend(self.cfg)
        self.demo2 = Demo2Backend(self.cfg)
        self.hosts: dict[str, HostBackend] = {
            "demo1": self.demo1,
            "demo2": self.demo2,
        }
        self.ktp = KtpBackend(self.cfg)
        self.share = Share(self.cfg)

    def pick_backend(self) -> str:
        """Prefer a configured idle host; use KTP when a deployment image is set."""
        for name, host in self.hosts.items():
            has_target = bool(host.spec.ssh) or os.environ.get("AB_LOCAL") == "1"
            if has_target and host.spec.bench and host.kernel_eval_count() == 0:
                return name
        if self.cfg.ktp_image:
            return "ktp"
        raise ValueError("Configure an A/B host or KB_KTP_IMAGE before selecting a backend")

    def layout(self) -> dict:
        """Configured host + KTP field table. No SSH."""
        return layout(self.cfg)

    def capacity(self, *, live: bool = True) -> dict:
        """layout() plus live KE / queue line (one SSH when live=True)."""
        cap = layout(self.cfg)
        if not live:
            return cap
        inv = self.share.inventory()
        try:
            ke = int(inv.get("KE") or 0)
        except ValueError:
            ke = -1
        used, deserved = parse_queue_used(inv.get("QUEUE_LINE") or "")
        cap["demo1"]["kernel_eval"] = ke
        try:
            cap["demo2"]["kernel_eval"] = self.demo2.kernel_eval_count()
        except Exception:
            cap["demo2"]["kernel_eval"] = -1
        cap["ktp"]["queue_used"] = used
        cap["ktp"]["queue_deserved"] = deserved
        cap["ktp"]["queue_line"] = inv.get("QUEUE_LINE") or ""
        cap["live"] = True
        return cap

    def plan(
        self,
        n_ops: int = 1,
        *,
        backend: str = "auto",
        live: bool = False,
        parallel: int | None = None,
    ) -> dict:
        """Fill Job/16-card fields. Agents pass n_ops; they do not invent npu/cpu."""
        if backend == "auto":
            if live:
                ke = int((self.capacity(live=True).get("demo1") or {}).get("kernel_eval") or 0)
                backend = "ktp" if ke > 0 else "demo1"
            else:
                backend = "ktp"
        if backend == "fleet":
            return fill_fleet(n_ops, self.cfg, ktp_parallel=parallel)
        if backend in ("demo1", "16card", "16"):
            out = fill_demo1_ab(self.cfg)
            out["note"] = "one ab910 on this host; extra ops go to plan(n, backend='ktp') or demo2"
            return out
        if backend == "demo2":
            out = fill_host_ab("demo2", self.cfg)
            out["note"] = "one ab910 on demo2; never two ab910 on the same host"
            return out
        return fill_ktp_job(n_ops, self.cfg, parallel=parallel)

    def ab(
        self,
        spec: AbSpec,
        *,
        backend: str = "auto",
        **kwargs,
    ) -> AbLaunch:
        be = self.pick_backend() if backend == "auto" else backend
        if be == "ktp":
            for k in ("devices", "detach", "force", "timeout"):
                kwargs.pop(k, None)
            return self.ktp.submit_ab(spec, **kwargs)
        if be in ("demo1", "16card", "16", "demo2"):
            for k in ("sync", "allow_wide", "stamp_name"):
                kwargs.pop(k, None)
            host = "demo2" if be == "demo2" else "demo1"
            return self.hosts[host].submit_ab(spec, **kwargs)
        raise ValueError(f"backend must be auto|ktp|demo1|demo2, got {be!r}")

    def run_ab(self, spec: AbSpec, *, backend: str = "auto", **kwargs) -> AbLaunch:
        """Alias of ab(). SDK demo1 used to expose only submit_ab."""
        return self.ab(spec, backend=backend, **kwargs)

    def wait(self, job_id: str, **kwargs) -> dict:
        return self.ktp.wait_done(job_id, **kwargs)

    def batch(
        self,
        specs: list[AbSpec],
        *,
        backend: str = "ktp",
        parallel: int = 1,
        wait: bool = False,
        **kwargs,
    ) -> list[AbLaunch]:
        if backend == "auto":
            backend = "ktp"
        if backend != "ktp":
            raise ValueError("batch is KTP-only (one operator, one Job, npu=2)")
        return self.ktp.batch_ab(specs, parallel=parallel, wait=wait, **kwargs)

    def cases_catalog(self, op: str) -> list[dict]:
        return [s.__dict__ for s in cases_mod.catalog(op)]

    def pull_run(self, tag: str, *, analyze: bool = True) -> dict:
        """Per-case numbers from a KTP/share A/B tag under the configured share runs directory."""
        raw = cases_mod.pull_share_run(tag, self.cfg)
        if analyze and raw.get("op"):
            raw["analysis"] = cases_mod.analyze_arms(raw.get("arms") or {}, raw.get("op"))
        elif analyze:
            raw["analysis"] = cases_mod.analyze_arms(raw.get("arms") or {}, None)
        return raw

    def pull_job(self, job_id: str) -> dict:
        """Official job per-case (wave.job_meta). Zero extra credit beyond GET /api/jobs."""
        return cases_mod.pull_job(job_id)

    def analyze_eval_files(self, files: dict[str, str], op: str) -> dict:
        """files = {arm: path/to/cann_final_eval.json}."""
        arms = {}
        for arm, path in files.items():
            parsed = cases_mod.parse_eval_file(path)
            arms[arm] = cases_mod._serialize_arms({arm: parsed["operators"]})[arm]
        return {"op": op, "arms": arms, "analysis": cases_mod.analyze_arms(arms, op)}

    def warmup(self) -> dict:
        import time
        t0 = time.time()
        inv = self.share.sync_tools()
        try:
            ke = int(inv.get("KE") or 0)
        except ValueError:
            ke = -1
        used, deserved = parse_queue_used(inv.get("QUEUE_LINE") or "")
        return {
            "s": round(time.time() - t0, 2),
            "share": self.cfg.share_root,
            "queue_line": inv.get("QUEUE_LINE") or "",
            "queue_used": used,
            "queue_deserved": deserved,
            "kernel_eval": ke,
            "layout": layout(self.cfg),
        }
