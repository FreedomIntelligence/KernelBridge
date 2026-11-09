"""SLAI SOCKS5 connect + OpenSSH ProxyCommand relay (binary-safe)."""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
from typing import Iterable

from .config import SocksNode


def consume_reply_addr(sock: socket.socket, atype: int) -> None:
    if atype == 1:
        sock.recv(6)
    elif atype == 3:
        ln = sock.recv(1)
        if not ln:
            raise OSError("short socks addr")
        sock.recv(ln[0] + 2)
    elif atype == 4:
        sock.recv(18)
    else:
        raise OSError(f"bad socks atype {atype}")


def socks5_connect(node: SocksNode, dest_host: str, dest_port: int, timeout: float = 6) -> socket.socket:
    sock = socket.create_connection((node.host, node.port), timeout)
    sock.sendall(b"\x05\x01\x02")
    greet = sock.recv(2)
    if greet != b"\x05\x02":
        sock.close()
        raise OSError(f"{node.name}: bad greet {greet!r}")
    u, p = node.user.encode(), node.password.encode()
    sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
    auth = sock.recv(2)
    if auth != b"\x01\x00":
        sock.close()
        raise OSError(f"{node.name}: auth failed {auth!r}")
    host_b = dest_host.encode("ascii")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + struct.pack("!H", dest_port))
    hdr = sock.recv(4)
    if len(hdr) < 4 or hdr[1] != 0:
        sock.close()
        raise OSError(f"{node.name}: connect failed {hdr!r}")
    consume_reply_addr(sock, hdr[3])
    return sock


def connect_first(nodes: Iterable[SocksNode], dest_host: str, dest_port: int, timeout: float = 6) -> tuple[SocksNode, socket.socket]:
    errors: list[str] = []
    for node in nodes:
        try:
            return node, socks5_connect(node, dest_host, dest_port, timeout)
        except OSError as exc:
            errors.append(f"{node.name}: {exc}")
    raise OSError("all SLAI nodes failed: " + "; ".join(errors))


def peek_ssh_banner(sock: socket.socket, timeout: float = 8) -> bytes:
    sock.settimeout(timeout)
    data = sock.recv(256)
    return data


def relay_stdio(sock: socket.socket) -> None:
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()

    def down() -> None:
        try:
            while True:
                data = sock.recv(16384)
                if not data:
                    break
                os.write(stdout_fd, data)
        except OSError:
            pass

    t = threading.Thread(target=down, daemon=True)
    t.start()
    try:
        while True:
            data = os.read(stdin_fd, 4096)
            if not data:
                break
            sock.sendall(data)
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    t.join(timeout=3)
    sock.close()
