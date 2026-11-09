"""OpenSSH via SLAI ProxyCommand. Do not route 31222 through Clash 7897."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .config import Config, PKG, load_config

# The campus Go SSH/SOCKS path does not reliably answer OpenSSH protocol probes.
# Keep long-lived sessions alive with remote stdout instead (school_sdk.keep).
SSH_BASE_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "UpdateHostKeys=no",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=0",
    "-o", "ServerAliveCountMax=3",
    "-o", "TCPKeepAlive=no",
    "-o", "ConnectTimeout=20",
    "-o", "IdentitiesOnly=yes",
)


def python_exe() -> str:
    return os.environ.get("SCHOOL_SDK_PYTHON") or sys.executable


def prefer_paramiko() -> bool:
    """Windows OpenSSH ProxyCommand stalls after the Go sshd accepts exec.

    SOCKS, KEX and authentication succeed, but command stdout never arrives.
    Paramiko over the same socket is the working command and keepalive path.
    SCHOOL_SSH_BACKEND=openssh|paramiko overrides the default.
    """
    backend = (os.environ.get("SCHOOL_SSH_BACKEND") or "").strip().lower()
    if backend == "openssh":
        return False
    if backend == "paramiko":
        return True
    return os.name == "nt"


def _load_paramiko():
    """Import Paramiko without its dependency's legacy-cipher warning noise."""
    import warnings
    from cryptography.utils import CryptographyDeprecationWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CryptographyDeprecationWarning)
        import paramiko
    return paramiko


def resolve_ssh_binary(*, platform: str | None = None) -> str:
    """Select one predictable OpenSSH implementation.

    Git Bash prepends its bundled ssh.exe to PATH.  Its process behaviour is
    different from Windows OpenSSH, so prefer the system binary on Windows.
    SCHOOL_SSH_BIN remains an explicit escape hatch.
    """
    override = os.environ.get("SCHOOL_SSH_BIN")
    if override:
        return override
    if (platform or os.name) == "nt":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        system_ssh = windir / "System32" / "OpenSSH" / "ssh.exe"
        if system_ssh.is_file():
            return str(system_ssh)
    return shutil.which("ssh") or "ssh"


def proxy_command(cfg: Config | None = None) -> str:
    cfg = cfg or load_config()
    script = PKG / "socks_cmd.py"
    py = python_exe()
    if os.name == "nt":
        return f'"{py}" -u "{script}" %h %p'
    return f"{py} -u {script} %h %p"


def ssh_argv(remote_cmd: str | None = None, cfg: Config | None = None) -> list[str]:
    cfg = cfg or load_config()
    ssh = resolve_ssh_binary()
    argv = [
        ssh,
        *SSH_BASE_OPTS,
        "-o", f"IdentityFile={cfg.identity}",
        "-o", f"ProxyCommand={proxy_command(cfg)}",
        "-p", str(cfg.ssh_port),
        cfg.ssh_target,
    ]
    if remote_cmd is not None:
        argv.append(remote_cmd)
    return argv


def render_ssh_block(cfg: Config) -> str:
    return (
        f"Host {cfg.alias}\n"
        f"    HostName {cfg.school_host}\n"
        f"    User {cfg.ssh_user}\n"
        f"    Port {cfg.ssh_port}\n"
        f"    IdentityFile {cfg.identity}\n"
        f"    IdentitiesOnly yes\n"
        f"    ProxyCommand {proxy_command(cfg)}\n"
        f"    ServerAliveInterval 0\n"
        f"    ServerAliveCountMax 3\n"
        f"    TCPKeepAlive no\n"
        f"    UpdateHostkeys no\n"
        f"    StrictHostKeyChecking accept-new\n"
    )


def upsert_ssh_config(text: str, block: str, alias: str) -> str:
    pattern = re.compile(
        rf"(?ms)^Host[ \t]+{re.escape(alias)}\n(?:[ \t]+.+\n)*(?:[ \t]+.+)?"
    )
    block = block.rstrip() + "\n"
    if pattern.search(text):
        return pattern.sub(lambda _m: block, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    return text + block


def write_ssh_config_block(cfg: Config | None = None, dest: Path | None = None) -> Path:
    cfg = cfg or load_config()
    dest = dest or (Path.home() / ".ssh" / "config")
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = dest.read_text(encoding="utf-8") if dest.is_file() else ""
    dest.write_text(upsert_ssh_config(existing, render_ssh_block(cfg), cfg.alias), encoding="utf-8")
    return dest


def _paramiko_exec(
    remote_cmd: str,
    cfg: Config,
    timeout: int,
    stdin: bytes | None,
) -> subprocess.CompletedProcess[bytes]:
    paramiko = _load_paramiko()
    from .socks import connect_first

    sock = None
    client = None
    try:
        _node, sock = connect_first(cfg.nodes, cfg.school_host, cfg.ssh_port)
        sock.settimeout(max(1.0, float(timeout)))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        banner = min(20.0, float(timeout))
        client.connect(
            hostname=cfg.school_host,
            port=cfg.ssh_port,
            username=cfg.ssh_user,
            key_filename=str(cfg.identity),
            sock=sock,
            timeout=timeout,
            banner_timeout=banner,
            auth_timeout=banner,
            allow_agent=False,
            look_for_keys=False,
        )
        transport = client.get_transport()
        if transport is None:
            raise OSError("paramiko transport missing after connect")
        chan = transport.open_session(timeout=timeout)
        chan.settimeout(timeout)
        chan.exec_command(remote_cmd)
        if stdin:
            chan.sendall(stdin)
        chan.shutdown_write()
        out_b = chan.makefile("rb").read() or b""
        err_b = chan.makefile_stderr("rb").read() or b""
        rc = chan.recv_exit_status()
        return subprocess.CompletedProcess(
            args=["paramiko", cfg.ssh_target, remote_cmd],
            returncode=rc,
            stdout=out_b,
            stderr=err_b,
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def ssh_exec(
    remote_cmd: str,
    cfg: Config | None = None,
    timeout: int = 60,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a remote command. Bytes in/out. Paramiko-over-SOCKS on Windows."""
    cfg = cfg or load_config()
    if prefer_paramiko():
        try:
            return _paramiko_exec(remote_cmd, cfg, timeout, stdin)
        except (socket.timeout, TimeoutError) as exc:
            raise subprocess.TimeoutExpired(
                ["paramiko", cfg.ssh_target, remote_cmd], timeout
            ) from exc
    argv = ssh_argv(remote_cmd, cfg)
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        timeout=timeout,
    )


def ssh(remote_cmd: str, cfg: Config | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    proc = ssh_exec(remote_cmd, cfg=cfg, timeout=timeout)
    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout=_blob_text(proc.stdout),
        stderr=_blob_text(proc.stderr),
    )


def _blob_text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


def ssh_ok(proc: subprocess.CompletedProcess[str]) -> bool:
    """Campus sshd often returns 255 even when the command ran.

    Windows OpenSSH additionally surfaces that as signed -1 / unsigned
    0xFFFFFFFF, so a 13s success was reported as failure.
    """
    if proc.returncode == 0:
        return True
    out = bool((proc.stdout or "").strip())
    return out and proc.returncode in (255, -1, 0xFFFFFFFF)
