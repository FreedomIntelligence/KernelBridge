"""A/B execution on a configured host; only one measurement job per host."""
from __future__ import annotations

import os
import subprocess
import time
from .config import AB910, Config, HostSpec
from .job import AbLaunch, AbSpec
from .parse import parse_detach_log, parse_runs, summarize
from .ssh import current_host_name, ssh


class HostBackend:
    def __init__(self, name: str = "demo1", cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.name = name
        self.spec: HostSpec = self.cfg.host(name)

    def kernel_eval_count(self) -> int:
        r = ssh(
            'ps -eo command | grep -c "[k]ernel_eval" || true',
            self.cfg,
            timeout=30,
            host=self.spec.ssh,
        )
        try:
            return int((r.stdout or "0").strip().splitlines()[-1].strip() or 0)
        except ValueError:
            return -1

    def kernel_eval_lines(self) -> str:
        r = ssh(
            'ps -eo etime,command | grep "[k]ernel_eval" | cut -c1-110 || true',
            self.cfg,
            timeout=30,
            host=self.spec.ssh,
        )
        return r.stdout

    def submit_ab(
        self,
        spec: AbSpec,
        *,
        devices: tuple[int, ...] | None = None,
        detach: bool = True,
        force: bool = False,
        cann: str | None = None,
        timeout: int | None = None,
        dry_run: bool = False,
    ) -> AbLaunch:
        if not AB910.is_file():
            raise FileNotFoundError(AB910)
        devs = devices or spec.devices or self.spec.prefer_devs[:2]
        for d in devs:
            if d in self.spec.skip_devs:
                raise ValueError(f"device {d} is in skip list {self.spec.skip_devs}")
        try:
            arm_args = spec.arm_specs()
        except FileNotFoundError:
            if not dry_run:
                raise
            arm_args = [f"{n}:{p}" for n, p in spec.arms.items()]
        cmd = ["bash", str(AB910), spec.op, str(spec.reps), *arm_args]
        if dry_run:
            return AbLaunch(
                backend=self.name,
                spec=spec,
                command=cmd,
                extra={
                    "dry_run": True,
                    "devices": list(devs),
                    "detach": detach,
                    "command": cmd,
                    "host": self.name,
                },
            )
        if not self.spec.bench:
            raise ValueError("Configure the host bench path (KB_AB_BENCH or HostSpec.bench)")
        if not self.spec.ssh and os.environ.get("AB_LOCAL") != "1":
            raise ValueError("Configure the host SSH target before launching A/B")
        ke = self.kernel_eval_count()
        if ke > 0 and not force:
            raise RuntimeError(
                f"{self.name} has {ke} kernel_eval process(es); a second ab910 "
                "on the same host will steal cards and zero timings. Use the "
                "other 16-card host, or backend='ktp' (one op, one Job, npu=2). "
                "AB_FORCE is not a fix."
            )
        env = os.environ.copy()
        env["AB_DEVS"] = " ".join(str(d) for d in devs)
        env["AB_CONC"] = str(len(devs))
        env["AB_DETACH"] = "1" if detach else "0"
        env["AB_TAG"] = spec.tag or str(int(time.time()))
        env["AB_CANN"] = cann if cann is not None else spec.cann
        env["CANN_BUILD_JOBS"] = str(spec.build_jobs)
        env["AB_SSH_USER"] = self.spec.user
        env["AB_SSH_HOST"] = self.spec.hostname
        env["AB_SSH_PORT"] = str(self.spec.port)
        env["CANN_BENCH_HOME"] = self.spec.bench
        if current_host_name() == self.name:
            env["AB_LOCAL"] = "1"
        if force:
            env["AB_FORCE"] = "1"
        wait = timeout if timeout is not None else (0 if detach else 7200)
        p = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=(wait or None),
        )
        text = (p.stdout or "") + (p.stderr or "")
        log = parse_detach_log(text)
        return AbLaunch(
            backend=self.name,
            spec=spec,
            log_path=log,
            command=cmd,
            extra={
                "rc": p.returncode,
                "devices": list(devs),
                "detach": detach,
                "ke": ke,
                "host": self.name,
                "ssh": self.spec.ssh,
                "stdout_tail": text[-4000:],
                "runs": [r.__dict__ for r in parse_runs(text)],
            },
        )

    def run_ab(self, spec: AbSpec, **kwargs) -> AbLaunch:
        """Alias of submit_ab. Older call sites expected demo1.run_ab."""
        return self.submit_ab(spec, **kwargs)

    def poll_log(self, log_path: str, tail: int = 80) -> str:
        r = ssh(
            f"tail -n {int(tail)} {log_path} 2>/dev/null || true",
            self.cfg,
            host=self.spec.ssh,
        )
        return r.stdout

    def collect(self, text: str) -> dict:
        lines = parse_runs(text)
        summ = summarize(lines)
        return {
            "n_lines": len(lines),
            "arms": {
                k: {
                    "n_clean": v.n_clean,
                    "min_score": v.min_score,
                    "scores": v.scores,
                }
                for k, v in summ.items()
            },
        }


class Demo1Backend(HostBackend):
    def __init__(self, cfg: Config | None = None):
        super().__init__("demo1", cfg)


class Demo2Backend(HostBackend):
    def __init__(self, cfg: Config | None = None):
        super().__init__("demo2", cfg)
