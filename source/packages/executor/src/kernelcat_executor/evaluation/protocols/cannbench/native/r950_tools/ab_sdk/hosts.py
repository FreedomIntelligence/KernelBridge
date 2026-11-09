"""16+16 host helpers: probe, bootstrap demo2 from demo1."""
from __future__ import annotations

from .config import SHARE_BENCH, Config
from .ssh import ssh


def probe_host(name: str, cfg: Config | None = None) -> dict:
    cfg = cfg or Config()
    spec = cfg.host(name)
    r = ssh(
        "echo USER=$(whoami); "
        "echo HOME=$HOME; "
        "python3 -V 2>/dev/null; "
        "python3 -c 'import torch,torch_npu; print(\"npu_count=%s\" % torch.npu.device_count())' 2>/dev/null; "
        f"echo BENCH=$([ -d {spec.bench} ] && echo 1 || echo 0); "
        f"echo SHARE_BENCH=$([ -d {SHARE_BENCH}/scripts ] && echo 1 || echo 0); "
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
    """Rebuild demo2 eval stack from the known-good torch_npu pack."""
    return rebuild_eval_env("demo2", cfg)


def rebuild_eval_env(name: str, cfg: Config | None = None) -> dict:
    """Reinstall torch 2.10 + torch_npu 2.10, AUTOLOAD=0, share bench symlink.

    Pack: /models/share/ka/ext/torch_npu_py311.tgz (torch 2.10.0+cpu + npu 2.10.0.post2).
    Do not pack from a dirty 2.13+cu130 host.
    """
    from pathlib import Path
    from .ssh import scp_to

    cfg = cfg or Config()
    spec = cfg.host(name)
    here = Path(__file__).resolve().parent.parent
    script = here / "rebuild_eval_env.sh"
    if not script.is_file():
        raise FileNotFoundError(script)
    remote = f"/tmp/rebuild_eval_env_{name}.sh"
    scp_to(script, remote, cfg, host=spec.ssh)
    r = ssh(
        f"bash {remote} {name}; echo REBUILD_RC=$?",
        cfg,
        timeout=300,
        host=spec.ssh,
    )
    rec = probe_host(name, cfg)
    rec["rebuild_log"] = r.stdout[-2500:]
    rec["rebuild_ok"] = "VERIFY_OK" in r.stdout
    rec["bootstrapped"] = rec["rebuild_ok"]
    return rec
