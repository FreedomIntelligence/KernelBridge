"""Keep Clash mixed-port 7897; stop Windows from bypassing 10.* (school intranet)."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from .config import DEFAULT_BYPASS

TEN_STAR = re.compile(r"(?:^|;)10\.\*(?:;|$)")


def strip_ten_star(bypass: str) -> str:
    parts = [p for p in bypass.split(";") if p and p != "10.*"]
    return ";".join(parts)


@dataclass
class SystemProxy:
    enable: bool
    server: str
    override: str


def read_system_proxy() -> SystemProxy | None:
    if os.name != "nt":
        return None
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
    )
    try:
        enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
    except FileNotFoundError:
        enable = 0
    try:
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except FileNotFoundError:
        server = ""
    try:
        override, _ = winreg.QueryValueEx(key, "ProxyOverride")
    except FileNotFoundError:
        override = ""
    winreg.CloseKey(key)
    return SystemProxy(bool(enable), str(server), str(override))


def _notify_wininet() -> None:
    import ctypes

    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(None, 39, None, 0)
    wininet.InternetSetOptionW(None, 37, None, 0)


def apply_system_proxy(clash_port: int = 7897, bypass: str | None = None) -> SystemProxy:
    """Enable 127.0.0.1:clash_port and drop 10.* from ProxyOverride."""
    if os.name != "nt":
        raise RuntimeError("apply_system_proxy is Windows-only")
    import winreg

    override = strip_ten_star(bypass or DEFAULT_BYPASS)
    server = f"127.0.0.1:{clash_port}"
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        0,
        winreg.KEY_SET_VALUE,
    )
    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
    winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
    winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, override)
    winreg.CloseKey(key)
    _notify_wininet()
    return SystemProxy(True, server, override)


def describe_proxy(clash_port: int = 7897) -> str:
    if os.name != "nt":
        return f"non-Windows: use Clash mixed-port 127.0.0.1:{clash_port}; do not bypass 10.1.30.201"
    cur = read_system_proxy()
    if cur is None:
        return "no proxy registry"
    lines = [
        f"ProxyEnable={int(cur.enable)}",
        f"ProxyServer={cur.server}",
        f"ProxyOverride={cur.override}",
    ]
    if "10.*" in cur.override.split(";"):
        lines.append("WARN: 10.* is bypassed — browser will not reach 10.1.30.201 via Clash")
    if cur.server and not cur.server.endswith(f":{clash_port}"):
        lines.append(f"WARN: system proxy is not 127.0.0.1:{clash_port}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe_proxy())
    if "--apply" in sys.argv:
        apply_system_proxy()
        print("--- after ---")
        print(describe_proxy())
