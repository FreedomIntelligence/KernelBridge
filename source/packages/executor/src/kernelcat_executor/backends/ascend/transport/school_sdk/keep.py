"""Background SSH keepalive. Reconnects 5s after drop."""
from __future__ import annotations

import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .config import STATE_DIR, Config, load_config
from .ssh import _load_paramiko, prefer_paramiko, ssh_argv

PID_FILE = STATE_DIR / "keep.pid"
LOCK_FILE = STATE_DIR / "keep.lock"
LOG_FILE = STATE_DIR / "keep.log"
REMOTE_KEEP = "while true; do echo keepalive $(date -Is); sleep 20; done"
HEARTBEAT_TIMEOUT_S = 45.0


@dataclass
class KeepStatus:
    running: bool
    pid: int | None
    log: Path


def _log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _pid_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_run_lock() -> BinaryIO | None:
    """Hold a process-scoped lock so two watchdogs cannot overlap."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open("a+b")
    if fh.seek(0, os.SEEK_END) == 0:
        fh.write(b"0")
        fh.flush()
    fh.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _release_run_lock(fh: BinaryIO) -> None:
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _terminate_process_tree(proc: subprocess.Popen[str]) -> int:
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            proc.terminate()
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=5)


def _run_heartbeat_session(argv: list[str], heartbeat_timeout: float = HEARTBEAT_TIMEOUT_S) -> int:
    """Stream remote heartbeats and reap an SSH process that only looks alive."""
    creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation,
    )
    assert proc.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def read_lines() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=read_lines, name="school-keep-reader", daemon=True).start()
    deadline = time.monotonic() + heartbeat_timeout
    while True:
        try:
            line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            _log(f"application heartbeat timeout after {heartbeat_timeout:g}s")
            return _terminate_process_tree(proc)
        if line is None:
            try:
                return proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return _terminate_process_tree(proc)
        clean = line.rstrip("\r\n")
        if clean:
            _log(f"ssh output: {clean}")
        if clean.startswith("keepalive "):
            deadline = time.monotonic() + heartbeat_timeout


def _run_paramiko_heartbeat(cfg: Config, heartbeat_timeout: float = HEARTBEAT_TIMEOUT_S) -> int:
    """Run the application heartbeat over the Windows-safe Paramiko backend."""
    from .socks import connect_first

    paramiko = _load_paramiko()
    sock = None
    client = None
    try:
        node, sock = connect_first(cfg.nodes, cfg.school_host, cfg.ssh_port)
        sock.settimeout(max(1.0, heartbeat_timeout))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=cfg.school_host,
            port=cfg.ssh_port,
            username=cfg.ssh_user,
            key_filename=str(cfg.identity),
            sock=sock,
            timeout=heartbeat_timeout,
            banner_timeout=min(20.0, heartbeat_timeout),
            auth_timeout=min(20.0, heartbeat_timeout),
            allow_agent=False,
            look_for_keys=False,
        )
        transport = client.get_transport()
        if transport is None:
            raise OSError("paramiko transport missing after connect")
        channel = transport.open_session(timeout=heartbeat_timeout)
        channel.settimeout(heartbeat_timeout)
        channel.exec_command(REMOTE_KEEP)
        stream = channel.makefile("rb")
        _log(f"paramiko connected node={node.name}")
        while True:
            try:
                raw = stream.readline()
            except (socket.timeout, TimeoutError):
                _log(f"application heartbeat timeout after {heartbeat_timeout:g}s")
                return 124
            if not raw:
                return channel.recv_exit_status() if channel.exit_status_ready() else 255
            clean = raw.decode("utf-8", "replace").rstrip("\r\n")
            if clean:
                _log(f"ssh output: {clean}")
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


def status() -> KeepStatus:
    if not PID_FILE.is_file():
        return KeepStatus(False, None, LOG_FILE)
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return KeepStatus(False, None, LOG_FILE)
    return KeepStatus(_pid_running(pid), pid, LOG_FILE)


def stop_keep() -> bool:
    st = status()
    if not st.running or st.pid is None:
        PID_FILE.unlink(missing_ok=True)
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(st.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            os.kill(st.pid, signal.SIGTERM)
    except OSError:
        pass
    PID_FILE.unlink(missing_ok=True)
    _log(f"watchdog stop pid={st.pid}")
    return True


def run_loop(cfg: Config | None = None) -> int:
    cfg = cfg or load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = _acquire_run_lock()
    if lock_fh is None:
        _log("watchdog duplicate refused")
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    _log(f"watchdog start pid={os.getpid()} target={cfg.ssh_target}:{cfg.ssh_port}")
    argv = ssh_argv(REMOTE_KEEP, cfg)
    try:
        while True:
            _log("ssh connect")
            try:
                if prefer_paramiko():
                    returncode = _run_paramiko_heartbeat(cfg)
                else:
                    returncode = _run_heartbeat_session(argv)
            except Exception as exc:
                _log(f"ssh launch failed: {exc}")
                returncode = 127
            _log(f"ssh exit={returncode}, retry in 5s")
            time.sleep(5)
    except KeyboardInterrupt:
        _log("watchdog interrupt")
        return 0
    finally:
        if PID_FILE.is_file() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
        _release_run_lock(lock_fh)


def start_keep(cfg: Config | None = None, *, detach: bool = True) -> KeepStatus:
    st = status()
    if st.running:
        return st
    if not detach:
        raise SystemExit(run_loop(cfg))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.unlink(missing_ok=True)
    creation = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW keeps stdin/stdout pipes valid; DETACHED_PROCESS
        # often kills the child ssh on Windows.
        creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | 0x08000000
    with LOG_FILE.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "school_sdk", "keep", "--foreground"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            creationflags=creation,
            start_new_session=(os.name != "nt"),
        )
    _log(f"watchdog detach pid={proc.pid}")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        st = status()
        if st.running and st.pid == proc.pid:
            return st
        if proc.poll() is not None:
            return KeepStatus(False, None, LOG_FILE)
        time.sleep(0.05)
    return KeepStatus(proc.poll() is None, proc.pid, LOG_FILE)
