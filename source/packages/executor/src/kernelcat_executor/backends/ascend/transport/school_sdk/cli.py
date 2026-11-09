"""CLI: python -m school_sdk  /  python schoolctl.py"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from .config import ENV_FILE, ROOT, Config, import_slai_yaml, load_config
from .keep import LOG_FILE, start_keep, status as keep_status, stop_keep
from .proxy_win import apply_system_proxy, describe_proxy
from .socks import connect_first, peek_ssh_banner, socks5_connect
from .ssh import prefer_paramiko, resolve_ssh_binary, ssh, ssh_ok, write_ssh_config_block


def _cfg() -> Config:
    return load_config()


def _cfg_user(user: str | None) -> Config:
    cfg = load_config()
    if not user or user == cfg.ssh_user:
        return cfg
    alias = {"demo1": "school-demo1", "demo2": "school-demo2"}.get(user, cfg.alias)
    return replace(cfg, ssh_user=user, alias=alias)


def cmd_setup(ns: argparse.Namespace) -> int:
    env_path = Path(ns.env) if ns.env else ENV_FILE
    if ns.from_yaml:
        src = Path(ns.from_yaml)
        nodes = import_slai_yaml(src)
        if not nodes:
            print(f"no socks5 nodes in {src}", file=sys.stderr)
            return 2
        lines = [
            f"SCHOOL_HOST={ns.host}",
            f"SCHOOL_SSH_PORT={ns.port}",
            f"SCHOOL_SSH_USER={ns.user}",
            f"CLASH_MIXED_PORT={ns.clash_port}",
            f"SLAI_USER={nodes[0].user}",
            f"SLAI_PASSWORD={nodes[0].password}",
            "SLAI_NODES=" + ",".join(f"{n.name}:{n.host}:{n.port}" for n in nodes),
        ]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {env_path} from {src}")
    elif not env_path.is_file():
        example = ROOT / "school.env.example"
        print(f"missing {env_path}. Copy {example} → school.env and set SLAI_PASSWORD.", file=sys.stderr)
        return 2
    cfg = load_config(env_path)
    dest = write_ssh_config_block(cfg)
    write_ssh_config_block(replace(cfg, ssh_user="demo1", alias="school-demo1"))
    write_ssh_config_block(replace(cfg, ssh_user="demo2", alias="school-demo2"))
    print(f"ssh config: {dest}  Host school-demo1, school-demo2")
    if ns.system_proxy:
        try:
            p = apply_system_proxy(cfg.clash_port)
            print(f"system proxy {p.server} (10.* no longer bypassed)")
        except RuntimeError as exc:
            print(exc)
    else:
        print(describe_proxy(cfg.clash_port))
    return 0


def cmd_proxy(ns: argparse.Namespace) -> int:
    cfg = _cfg()
    if ns.apply:
        try:
            apply_system_proxy(cfg.clash_port)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 2
    print(describe_proxy(cfg.clash_port))
    return 0


def cmd_probe(_ns: argparse.Namespace) -> int:
    cfg = _cfg()
    if not cfg.slai_password:
        print("SLAI_PASSWORD missing in school.env", file=sys.stderr)
        return 2
    print(f"clash expected 127.0.0.1:{cfg.clash_port} (HTTP/HTTPS only; not SSH 31222)")
    print(describe_proxy(cfg.clash_port))
    ok_any = False
    for node in cfg.nodes:
        print(f"--- {node.name} {node.host}:{node.port} ---")
        try:
            sock = socks5_connect(node, cfg.school_host, cfg.ssh_port)
            banner = peek_ssh_banner(sock)
            sock.close()
            print(f"31222 banner {banner!r}")
            ok_any = banner.startswith(b"SSH-")
        except OSError as exc:
            print(f"fail {exc}")
    try:
        s = socket.create_connection(("127.0.0.1", cfg.clash_port), 3)
        s.close()
        print(f"clash {cfg.clash_port} listen=yes")
    except OSError:
        print(f"clash {cfg.clash_port} listen=no")
    return 0 if ok_any else 1


def cmd_ssh(ns: argparse.Namespace) -> int:
    cfg = _cfg_user(getattr(ns, "user", None))
    remote = " ".join(ns.cmd) if ns.cmd else "echo SCHOOL_OK; whoami; hostname; ktp version 2>/dev/null || true"
    try:
        proc = ssh(remote, cfg, timeout=ns.timeout)
    except subprocess.TimeoutExpired:
        print(f"remote command timed out after {ns.timeout}s", file=sys.stderr)
        return 124
    sys.stdout.write(proc.stdout or "")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return 0 if ssh_ok(proc) else proc.returncode or 1


def cmd_keep(ns: argparse.Namespace) -> int:
    if ns.stop:
        print("stopped" if stop_keep() else "not running")
        return 0
    if ns.status:
        st = keep_status()
        print(f"running={st.running} pid={st.pid} log={st.log}")
        return 0
    if ns.foreground:
        from .keep import run_loop
        return run_loop()
    st = start_keep(detach=True)
    print(f"keep running={st.running} pid={st.pid} log={st.log}")
    return 0 if st.running else 1


def _keep_log_hint(n: int = 40) -> str:
    if not LOG_FILE.is_file():
        return "keep.log missing"
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    timeouts = sum(1 for line in lines if "Timeout, server" in line)
    exits = [line for line in lines if "ssh exit=" in line]
    last = lines[-1] if lines else ""
    return f"keep.log last={last!r} timeout_hits={timeouts} last_exit={exits[-1] if exits else '-'}"


def _session_hint() -> str:
    """Count overlapping SOCKS/SSH sessions that starve a new login or cat-put."""
    if os.name != "nt":
        return "sessions=n/a"
    try:
        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { "
                "($_.Name -eq 'ssh.exe' -and $_.CommandLine -match 'school-demo2|10\\.1\\.30\\.201') -or "
                "($_.Name -eq 'python.exe' -and $_.CommandLine -match 'socks_cmd\\.py|-m school_sdk keep --foreground') } | "
                "Measure-Object | Select-Object -ExpandProperty Count",
            ],
            text=True,
            timeout=12,
        ).strip()
        return f"overlapping_procs={raw}"
    except (OSError, subprocess.SubprocessError):
        return "overlapping_procs=?"


def cmd_status(_ns: argparse.Namespace) -> int:
    cfg = _cfg()
    print(f"target {cfg.ssh_target}:{cfg.ssh_port} alias={cfg.alias}")
    print(f"nodes {[n.name for n in cfg.nodes]}")
    print(describe_proxy(cfg.clash_port))
    st = keep_status()
    print(f"keep running={st.running} pid={st.pid}")
    print(_keep_log_hint())
    print(_session_hint())
    ssh_bin = resolve_ssh_binary()
    backend = "paramiko" if prefer_paramiko() else "openssh"
    print(f"ssh_backend={backend} openssh={ssh_bin}")
    return 0


def cmd_ktp(ns: argparse.Namespace) -> int:
    remote = "ktp " + " ".join(ns.cmd) if ns.cmd else "ktp status"
    try:
        proc = ssh(remote, timeout=ns.timeout)
    except subprocess.TimeoutExpired:
        print(f"remote command timed out after {ns.timeout}s", file=sys.stderr)
        return 124
    sys.stdout.write(proc.stdout or "")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return 0 if ssh_ok(proc) else proc.returncode or 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="school_sdk",
        description="Keep Clash on 7897; SSH demo2@10.1.30.201:31222 via SLAI SOCKS5",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="write ~/.ssh/config + optional school.env from yaml")
    p_setup.add_argument("--from-yaml", help="import slai_alibaba.yaml into school.env")
    p_setup.add_argument("--env", default="")
    p_setup.add_argument("--host", default="10.1.30.201")
    p_setup.add_argument("--port", type=int, default=31222)
    p_setup.add_argument("--user", default="demo2")
    p_setup.add_argument("--clash-port", type=int, default=7897)
    p_setup.add_argument("--system-proxy", action="store_true", help="Windows: enable 127.0.0.1:7897, drop 10.* bypass")
    p_setup.set_defaults(func=cmd_setup)

    p_proxy = sub.add_parser("proxy", help="show/apply Windows system proxy (keep 7897)")
    p_proxy.add_argument("--apply", action="store_true")
    p_proxy.set_defaults(func=cmd_proxy)

    p_probe = sub.add_parser("probe", help="test SLAI SOCKS → :31222 SSH banner")
    p_probe.set_defaults(func=cmd_probe)

    p_ssh = sub.add_parser("ssh", help="run a command on demo1 or demo2")
    p_ssh.add_argument("cmd", nargs="*")
    p_ssh.add_argument("--user", default="", help="demo1 or demo2 (default: SCHOOL_SSH_USER)")
    p_ssh.add_argument("--timeout", type=int, default=60)
    p_ssh.set_defaults(func=cmd_ssh)

    p_keep = sub.add_parser("keep", help="background SSH keepalive")
    p_keep.add_argument("--foreground", action="store_true")
    p_keep.add_argument("--stop", action="store_true")
    p_keep.add_argument("--status", action="store_true")
    p_keep.set_defaults(func=cmd_keep)

    p_st = sub.add_parser("status", help="proxy + keep + config")
    p_st.set_defaults(func=cmd_status)

    p_ktp = sub.add_parser("ktp", help="ktp … on demo2")
    p_ktp.add_argument("cmd", nargs="*")
    p_ktp.add_argument("--timeout", type=int, default=90)
    p_ktp.set_defaults(func=cmd_ktp)

    ns = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    return int(ns.func(ns))
