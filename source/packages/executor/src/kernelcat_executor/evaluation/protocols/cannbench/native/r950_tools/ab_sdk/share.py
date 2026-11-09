"""Put A/B code on /models/share (same 500T as /mnt/host-model). Never /mnt/persistent."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from .config import AB910, CAMPAIGN, LIVESRC, TEMPLATE, Config
from .job import AbSpec, guard_path
from .ssh import on_codeserver, scp_to, ssh


def to_share_path(local: str | Path, cfg: Config | None = None) -> str:
    cfg = cfg or Config()
    p = Path(local).expanduser().resolve()
    campaign = CAMPAIGN.resolve()
    # The canonical parent assets now live outside the backend source tree.
    # Preserve their established remote identity through the resource link.
    if p.is_relative_to(LIVESRC.resolve()):
        rel = p.relative_to(LIVESRC.resolve())
        return f"{cfg.share_campaign}/r950_tools/livesrc910/{rel.as_posix()}"
    try:
        rel = p.relative_to(campaign)
        return f"{cfg.share_campaign}/{rel.as_posix()}"
    except ValueError:
        # External task arms are very commonly all named ``arm``.  Mapping by
        # basename made unrelated policies share ``.../ext/arm``; rsync_dir's
        # overlay extraction then retained stale operator trees and a later
        # sync could also replace a queued job's source.  Bind the cache path
        # to the canonical local identity while keeping it readable.
        identity = os.path.normcase(str(p)).replace("\\", "/")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", p.name).strip("-.") or "src"
        return f"{cfg.share_root}/ext/{stem}-{digest}"


class Share:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()

    def mkdir(self) -> None:
        self.inventory()

    def inventory(self) -> dict:
        """One SSH: mkdir, optional bench copy, stamps, sizes."""
        cfg = self.cfg
        r = ssh(
            f"mkdir -p {cfg.share_root} {cfg.share_runs} {cfg.share_jobs} "
            f"{cfg.share_campaign} {cfg.share_root}/ext; "
            f"echo BENCH=$([ -d {cfg.share_bench}/scripts ] && echo 1 || echo 0); "
            f"echo TASKS=$([ -d {cfg.share_bench}/tasks ] && echo 1 || echo 0); "
            f"echo AB910=$(stat -c %s {cfg.share_campaign}/r950_tools/ab910.sh 2>/dev/null || echo 0); "
            f"echo STAMP_TPL=$(cat {cfg.share_campaign}/cann_submit/template/.ka_stamp 2>/dev/null); "
            f"echo STAMP_SDK=$(cat {cfg.share_campaign}/r950_tools/ab_sdk/.ka_stamp 2>/dev/null); "
            f"echo KE=$(ps -eo command 2>/dev/null | grep -c '[k]ernel_eval' || true); "
            f"source ~/.ktp/env >/dev/null 2>&1 || true; "
            f"echo 'QUEUE_LINE='$(ktp queues </dev/null 2>/dev/null | grep normal-queue | tr -s ' ' | head -1); "
            f"echo INV_OK",
            self.cfg,
            timeout=45,
        )
        if "INV_OK" not in r.stdout:
            raise RuntimeError(f"share inventory failed: {r.text[-500:]}")
        out = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
        return out

    def ensure_bench(self, inv: dict | None = None) -> None:
        if inv is None:
            inv = self.inventory()
        if inv.get("BENCH") != "1":
            raise RuntimeError("cann-bench missing on share after inventory")

    def remote_size(self, path: str) -> int:
        r = ssh(f"stat -c %s {shlex.quote(path)} 2>/dev/null || echo 0", self.cfg)
        try:
            return int((r.stdout or "0").strip().splitlines()[-1].strip() or 0)
        except ValueError:
            return -1

    def _push_file(self, src: Path, dst: str) -> None:
        parent = str(Path(dst).parent)
        local_sz = src.stat().st_size
        if self.remote_size(dst) == local_sz:
            return
        ssh(f"mkdir -p {shlex.quote(parent)} && echo MKDIR_OK", self.cfg)
        last = ""
        for _ in range(1, 4):
            scp_to(src, dst, self.cfg, timeout=120)
            last = str(self.remote_size(dst))
            if last == str(local_sz):
                return
        raise RuntimeError(f"scp {src} -> {dst} size {local_sz} vs remote {last}")

    def rsync_file(self, src: Path, dst: str) -> None:
        src = src.expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(src)
        guard_path(dst, self.cfg)
        if on_codeserver():
            parent = str(Path(dst).parent)
            ssh(f"mkdir -p {shlex.quote(parent)}", self.cfg)
            subprocess.run(["cp", "-a", str(src), dst], check=True)
            return
        self._push_file(src, dst)

    _SKIP_PARTS = frozenset({"__pycache__", ".git", "reports"})
    _SKIP_SUFFIX = frozenset({".pyc", ".done", ".log"})

    @staticmethod
    def dir_stamp(src: Path) -> str:
        """Cheap fingerprint. Cap walk — rglob of arms/ (80k files) hung batch_ab."""
        n = total = newest = 0
        for p in src.rglob("*"):
            if not p.is_file():
                continue
            if any(part in Share._SKIP_PARTS for part in p.parts):
                continue
            if p.suffix in Share._SKIP_SUFFIX:
                continue
            st = p.stat()
            n += 1
            total += st.st_size
            newest = max(newest, int(st.st_mtime))
            if n >= 4000:
                break
        return f"{n}:{total}:{newest}"

    def rsync_dir(self, src: Path, dst: str) -> None:
        src = src.expanduser().resolve()
        if not src.is_dir():
            raise FileNotFoundError(src)
        guard_path(dst, self.cfg)
        stamp = self.dir_stamp(src)
        remote_stamp = ssh(
            f"cat {shlex.quote(dst)}/.ka_stamp 2>/dev/null || true", self.cfg,
        ).stdout.strip()
        if remote_stamp == stamp:
            return
        if on_codeserver():
            ssh(f"mkdir -p {shlex.quote(dst)}", self.cfg)
            subprocess.run(
                ["rsync", "-a", "--exclude", "__pycache__", "--exclude", "*.pyc",
                 "--exclude", "*.done", "--exclude", "*.log",
                 f"{src}/", f"{dst}/"],
                check=True,
            )
            ssh(f"printf '%s\\n' {shlex.quote(stamp)} > {shlex.quote(dst)}/.ka_stamp", self.cfg)
            return
        fd, tmp = tempfile.mkstemp(suffix=".tgz")
        os.close(fd)
        tgz = Path(tmp)
        try:
            subprocess.run(
                ["tar", "czf", str(tgz),
                 "--exclude", "__pycache__", "--exclude", "*.pyc",
                 "--exclude", "*.done", "--exclude", "*.log",
                 "-C", str(src), "."],
                check=True,
            )
            remote_tgz = f"/tmp/_ka_{os.getpid()}_{src.name}.tgz"
            self._push_file(tgz, remote_tgz)
            r = ssh(
                f"mkdir -p {shlex.quote(dst)} && "
                f"tar xzf {shlex.quote(remote_tgz)} -C {shlex.quote(dst)} && "
                f"printf '%s\\n' {shlex.quote(stamp)} > {shlex.quote(dst)}/.ka_stamp && "
                f"rm -f {shlex.quote(remote_tgz)} && echo EXTRACT_OK",
                self.cfg,
                timeout=120,
            )
            if "EXTRACT_OK" not in r.stdout:
                raise RuntimeError(f"extract {src} -> {dst} failed: {r.text[-400:]}")
        finally:
            tgz.unlink(missing_ok=True)

    def sync_tools(self) -> dict:
        inv = self.inventory()
        ab_dst = f"{self.cfg.share_campaign}/r950_tools/ab910.sh"
        try:
            if int(inv.get("AB910") or 0) != AB910.stat().st_size:
                self.rsync_file(AB910, ab_dst)
        except ValueError:
            self.rsync_file(AB910, ab_dst)
        if inv.get("STAMP_TPL") != self.dir_stamp(TEMPLATE):
            self.rsync_dir(TEMPLATE, f"{self.cfg.share_campaign}/cann_submit/template")
        sdk = Path(__file__).resolve().parent
        if inv.get("STAMP_SDK") != self.dir_stamp(sdk):
            self.rsync_dir(sdk, f"{self.cfg.share_campaign}/r950_tools/ab_sdk")
        return inv

    def sync_spec(self, spec: AbSpec, *, tools: bool = True) -> dict[str, str]:
        """Copy each arm onto the share. Returns {arm: share_path}."""
        if tools:
            inv = self.sync_tools()
            self.ensure_bench(inv)
        mapped: dict[str, str] = {}
        for name, src in spec.arms.items():
            local = Path(src).expanduser().resolve()
            guard_path(str(local), self.cfg)
            remote = to_share_path(local, self.cfg)
            self.rsync_dir(local, remote)
            mapped[name] = remote
        return mapped

    def sync_specs(self, specs: list[AbSpec]) -> dict[str, dict[str, str]]:
        """One tools/bench copy, then unique arm directories only."""
        inv = self.sync_tools()
        self.ensure_bench(inv)
        out: dict[str, dict[str, str]] = {}
        done: set[str] = set()
        for spec in specs:
            mapped: dict[str, str] = {}
            for name, src in spec.arms.items():
                local = Path(src).expanduser().resolve()
                guard_path(str(local), self.cfg)
                remote = to_share_path(local, self.cfg)
                if remote not in done:
                    self.rsync_dir(local, remote)
                    done.add(remote)
                mapped[name] = remote
            key = spec.tag or spec.op
            out[key] = mapped
        return out
