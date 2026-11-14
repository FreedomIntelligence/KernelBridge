"""SSH transport for explicitly configured A/B hosts."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import SSH_OPTS, Config

_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+=*\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")


def redact(text: str) -> str:
    return _JWT.sub("eyJ…REDACTED", text)


def on_demo1() -> bool:
    return current_host_name() == "demo1"


def on_demo2() -> bool:
    return current_host_name() == "demo2"


def current_host_name() -> str | None:
    """Local execution is an explicit deployment property, not a path guess."""
    return os.environ.get("KB_AB_LOCAL_HOST") or None


def on_codeserver() -> bool:
    return local_exec()


def _target_user(ssh_host: str) -> str:
    return ssh_host.split("@", 1)[0] if "@" in ssh_host else ""


def local_exec(target_host: str | None = None, cfg: Config | None = None) -> bool:
    """Execute locally only when the caller explicitly selected local execution."""
    if os.environ.get("AB_LOCAL") == "1":
        return True
    cur = current_host_name()
    if cur is None:
        return False
    if target_host is None:
        return True
    cfg = cfg or Config()
    spec = cfg.hosts.get(cur)
    return spec is not None and bool(spec.ssh) and target_host == spec.ssh


def _iface_exists(name: str) -> bool:
    if Path(f"/sys/class/net/{name}").exists():
        return True
    try:
        p = subprocess.run(
            ["ifconfig", name],
            capture_output=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def school_proxy_opts() -> list[str]:
    return []


def effective_ssh_opts() -> list[str]:
    """Use the configured SSH options and omit unavailable bind interfaces."""
    out: list[str] = []
    opts = list(SSH_OPTS)
    i = 0
    while i < len(opts):
        a = opts[i]
        b = opts[i + 1] if i + 1 < len(opts) else ""
        if a == "-o" and b.startswith("BindInterface="):
            iface = b.split("=", 1)[1]
            if not _iface_exists(iface):
                i += 2
                continue
        out.append(a)
        i += 1
    if os.name == "nt":
        out.extend(school_proxy_opts())
    return out


def _decode(blob: bytes | str | None) -> str:
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    return blob.decode("utf-8", "replace")


def _paramiko_exec_fn():
    return None


def _school_cfg(ssh_host: str):
    """Paramiko must use the SSH username; demo1 and demo2 are different boxes."""
    from dataclasses import replace

    _root = Path(__file__).resolve().parents[6] / "backends/ascend/transport"
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from kernelcat_executor.backends.ascend.transport.direct_ssh import load_config

    user = _target_user(ssh_host)
    alias = {"demo1": "school-demo1", "demo2": "school-demo2"}.get(user, f"school-{user}")
    return replace(load_config(), ssh_user=user, alias=alias)


@dataclass
class CmdResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        if self.rc in (0, 255) and self.stdout.strip():
            return True
        return self.rc == 0

    @property
    def text(self) -> str:
        return (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")


def _run(argv: list[str], *, timeout: int, stdin: bytes | None = None) -> CmdResult:
    try:
        p = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CmdResult(
            124,
            _decode(exc.stdout),
            _decode(exc.stderr) + f"\nTIMEOUT {timeout}s argv={argv[:6]!r}",
        )
    return CmdResult(p.returncode, _decode(p.stdout), _decode(p.stderr))


def _ssh_target(cfg: Config, host: str | None) -> tuple[str, int]:
    if host is not None:
        if not host.strip():
            if os.environ.get("AB_LOCAL") == "1":
                return "", cfg.demo1_port
            raise ValueError("An explicitly selected A/B host has no SSH target")
        for spec in cfg.hosts.values():
            if spec.ssh and spec.ssh == host:
                return spec.ssh, spec.port
        if host in cfg.hosts:
            spec = cfg.host(host)
            if not spec.ssh:
                if os.environ.get("AB_LOCAL") == "1":
                    return "", spec.port
                raise ValueError(f"A/B host {host!r} has no SSH target")
            return spec.ssh, spec.port
        return host, cfg.demo1_port
    if not cfg.demo1_host and os.environ.get("AB_LOCAL") != "1":
        raise ValueError("Configure KB_AB_HOST (or Config.demo1_host) before using remote A/B tools")
    return cfg.demo1_host, cfg.demo1_port


def ssh(
    script: str,
    cfg: Config | None = None,
    timeout: int = 60,
    host: str | None = None,
) -> CmdResult:
    """Remote command as ssh argv. Do NOT use `bash -s` stdin: ktp reads stdin
    and deadlocks (batch_ab looked hung; leaked ssh bash -s). Long scripts
    are dropped to /tmp and executed by path.
    """
    cfg = cfg or Config()
    ssh_host, port = _ssh_target(cfg, host)
    if local_exec(ssh_host, cfg):
        return _run(["bash", "-lc", script], timeout=timeout)
    _sx = _paramiko_exec_fn()
    if _sx is not None:
        if len(script.encode("utf-8")) > 80_000:
            return _ssh_via_file(script, cfg, timeout, ssh_host=ssh_host, port=port)
        try:
            proc = _sx(script, cfg=_school_cfg(ssh_host), timeout=timeout)
        except Exception as exc:
            return CmdResult(rc=1, stdout="", stderr=f"paramiko ssh failed: {exc}")
        rc = proc.returncode
        ok = (rc == 0) or (bool((proc.stdout or b"").strip()) and rc in (255, -1, 0xFFFFFFFF))
        return CmdResult(rc=0 if ok else (rc or 1),
                         stdout=_decode(proc.stdout), stderr=_decode(proc.stderr))
    if len(script.encode("utf-8")) > 80_000:
        return _ssh_via_file(script, cfg, timeout, ssh_host=ssh_host, port=port)
    argv = [
        "ssh", *effective_ssh_opts(), "-p", str(port),
        ssh_host, script,
    ]
    return _run(argv, timeout=timeout)


def _ssh_via_file(
    script: str,
    cfg: Config,
    timeout: int,
    *,
    ssh_host: str,
    port: int,
) -> CmdResult:
    import tempfile
    import time as _time
    local_fd, local_name = tempfile.mkstemp(prefix="ka_ssh_", suffix=".sh")
    os.close(local_fd)
    local = Path(local_name)
    remote = f"/tmp/ka_ssh_{os.getpid()}_{int(_time.time())}.sh"
    try:
        local.write_text(script)
        scp = scp_to(local, remote, cfg, timeout=min(60, timeout), host=ssh_host)
        if scp.rc not in (0, 255):
            return scp
        return ssh(
            f"bash {remote}; rc=$?; rm -f {remote}; exit $rc",
            cfg, timeout=timeout, host=ssh_host,
        )
    finally:
        local.unlink(missing_ok=True)


def scp_to(
    local: Path,
    remote: str,
    cfg: Config | None = None,
    timeout: int = 120,
    host: str | None = None,
) -> CmdResult:
    cfg = cfg or Config()
    ssh_host, port = _ssh_target(cfg, host)
    if local_exec(ssh_host, cfg):
        remote_path = remote.split(":", 1)[-1] if ":" in remote else remote
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        return _run(["cp", "-a", str(local), remote_path], timeout=timeout)
    dest = f"{ssh_host}:{remote}" if ":" not in remote else remote
    _sx = _paramiko_exec_fn()
    if _sx is not None:
        # Windows OpenSSH 传输挂死：Paramiko-over-SOCKS（与 ab20_duration 同款）。
        import posixpath as _posixpath
        import shlex as _shlex
        dest_path = remote.split(":", 1)[-1] if ":" in remote else remote
        cmd = (f"mkdir -p {_shlex.quote(_posixpath.dirname(dest_path))} && "
               f"cat > {_shlex.quote(dest_path)}")
        try:
            proc = _sx(cmd, cfg=_school_cfg(ssh_host), timeout=timeout, stdin=Path(local).read_bytes())
            # paramiko 外层 rc 不可信（可能恒 -1/127）；无本地异常即视为送达，
            # 真实性由调用方的大小对账把关。
            return CmdResult(rc=0, stdout=_decode(proc.stdout), stderr=_decode(proc.stderr))
        except Exception as exc:
            return CmdResult(rc=1, stdout="", stderr=f"paramiko put failed: {exc}")
    # Windows（Git Bash）无 en0/直连路由，必须走 ~/.ssh/config 的 ProxyCommand；
    # AB_SCP_USE_CONFIG=1 时去掉 -F /dev/null，让 scp 读 ssh config。
    argv = ["scp"]
    if not os.environ.get("AB_SCP_USE_CONFIG"):
        argv += ["-F", "/dev/null"]
    argv += [
        "-O", *effective_ssh_opts(),
        "-P", str(port), str(local), dest,
    ]
    return _run(argv, timeout=timeout)


def ensure_master(cfg: Config | None = None, timeout: int = 25) -> None:
    """No-op. This host's sshd hangs on ControlMaster -fN."""
    return
