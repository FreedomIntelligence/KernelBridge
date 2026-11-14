"""Probe and bootstrap configured evaluation hosts."""
from __future__ import annotations

import shlex
from .config import Config
from .ssh import ssh


def probe_host(name: str, cfg: Config | None = None) -> dict:
    cfg = cfg or Config()
    spec = cfg.host(name)
    r = ssh(
        "echo USER=$(whoami); "
        "echo HOME=$HOME; "
        "python3 -V 2>/dev/null; "
        "python3 -c 'import torch,torch_npu; print(\"npu_count=%s\" % torch.npu.device_count())' 2>/dev/null; "
        f"echo BENCH=$([ -d {shlex.quote(spec.bench)} ] && echo 1 || echo 0); "
        f"echo SHARE_BENCH=$([ -d {shlex.quote(cfg.share_bench + '/scripts')} ] && echo 1 || echo 0); "
        "echo DAVINCI=$(ls /dev/davinci[0-9]* 2>/dev/null | wc -l); "
        "echo KE=$(ps -eo command 2>/dev/null | grep -c '[k]ernel_eval' || true); "
        "echo PROBE_OK",
        cfg,
        timeout=45,
        host=spec.ssh,
    )
    out = {"name": name, "ssh": spec.ssh, "ok": "PROBE_OK" in r.stdout, "raw": r.stdout[-1500:]}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def bootstrap_demo2(cfg: Config | None = None) -> dict:
    """Rebuild the secondary host using its configured package archive."""
    return rebuild_eval_env("demo2", cfg)


def rebuild_eval_env(name: str, cfg: Config | None = None) -> dict:
    """Reinstall an explicitly supplied package archive on a configured host."""
    from pathlib import Path
    from .ssh import scp_to

    cfg = cfg or Config()
    spec = cfg.host(name)
    here = Path(__file__).resolve().parent.parent
    script = here / "rebuild_eval_env.sh"
    if not script.is_file():
        raise FileNotFoundError(script)
    pack = cfg.extra_env.get("CANN_REBUILD_PACK")
    if not pack or not spec.bench:
        raise ValueError("Rebuild requires CANN_REBUILD_PACK in Config.extra_env and HostSpec.bench")
    import uuid
    remote = f"/tmp/rebuild_eval_env_{uuid.uuid4().hex}.sh"
    scp_to(script, remote, cfg, host=spec.ssh)
    overrides = {
        **cfg.extra_env,
        "CANN_REBUILD_PACK": pack,
        "CANN_BENCH_SOURCE": cfg.share_bench,
        "CANN_BENCH_HOME": spec.bench,
    }
    command = "env " + " ".join(shlex.quote(f"{k}={v}") for k, v in overrides.items())
    r = ssh(
        f"{command} bash {shlex.quote(remote)} {shlex.quote(name)}; echo REBUILD_RC=$?",
        cfg,
        timeout=300,
        host=spec.ssh,
    )
    rec = probe_host(name, cfg)
    rec["rebuild_log"] = r.stdout[-2500:]
    rec["rebuild_ok"] = "VERIFY_OK" in r.stdout
    rec["bootstrapped"] = rec["rebuild_ok"]
    return rec
