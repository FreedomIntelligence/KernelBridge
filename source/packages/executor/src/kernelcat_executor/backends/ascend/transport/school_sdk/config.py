"""Load school.env without committing secrets."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = Path(__file__).resolve().parent
ENV_FILE = ROOT / "school.env"
STATE_DIR = Path.home() / ".school_sdk"
DEFAULT_BYPASS = (
    "localhost;127.*;192.168.*;172.16.*;172.17.*;172.18.*;172.19.*;"
    "172.20.*;172.21.*;172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;"
    "172.27.*;172.28.*;172.29.*;172.30.*;172.31.*;127.0.0.1;<local>"
)


@dataclass(frozen=True)
class SocksNode:
    name: str
    host: str
    port: int
    user: str
    password: str


@dataclass(frozen=True)
class Config:
    school_host: str = "10.1.30.201"
    ssh_port: int = 31222
    ssh_user: str = "demo2"
    clash_port: int = 7897
    slai_user: str = "server"
    slai_password: str = ""
    nodes: tuple[SocksNode, ...] = ()
    identity: Path = Path.home() / ".ssh" / "id_ed25519"
    alias: str = "school-demo2"

    @property
    def ssh_target(self) -> str:
        return f"{self.ssh_user}@{self.school_host}"


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


# Tokyo:1080 in slai_alibaba.yaml is a dead SOCKS listener (auth \x01\x01).
# Leaving it first-or-second in SLAI_NODES wastes a 6–15s failover slot.
_DEAD_NODES = {("tokyo", 1080)}


def is_dead_node(node: SocksNode) -> bool:
    return (node.name.lower(), node.port) in _DEAD_NODES


def parse_nodes(spec: str, user: str, password: str) -> tuple[SocksNode, ...]:
    nodes: list[SocksNode] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3:
            raise ValueError(f"node must be name:host:port, got {part!r}")
        node = SocksNode(bits[0], bits[1], int(bits[2]), user, password)
        if is_dead_node(node):
            continue
        nodes.append(node)
    return tuple(nodes)


def import_slai_yaml(path: Path, user: str | None = None) -> list[SocksNode]:
    """Best-effort parse of a Clash-style slai_alibaba.yaml."""
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*-\s+name:\s*", text)
    nodes: list[SocksNode] = []
    default_user = user or "server"
    for block in blocks[1:]:
        name_m = re.match(r"(\S+)", block)
        if not name_m:
            continue
        name = name_m.group(1)
        typ = _yaml_field(block, "type")
        if typ and typ != "socks5":
            continue
        host = _yaml_field(block, "server")
        port_s = _yaml_field(block, "port")
        u = _yaml_field(block, "username") or default_user
        pw = _yaml_field(block, "password") or ""
        if host and port_s:
            node = SocksNode(name, host, int(port_s), u, pw)
            if is_dead_node(node):
                continue
            nodes.append(node)
    return nodes


def _yaml_field(block: str, key: str) -> str:
    m = re.search(rf"(?m)^\s*{re.escape(key)}:\s*(.+?)\s*$", block)
    if not m:
        return ""
    return m.group(1).strip().strip("'").strip('"')


def load_config(env_path: Path | None = None) -> Config:
    data = _parse_env_file(env_path or ENV_FILE)
    merged = {**data, **{k: v for k, v in os.environ.items() if k.startswith(("SCHOOL_", "SLAI_", "CLASH_"))}}
    user = merged.get("SLAI_USER", "server")
    password = merged.get("SLAI_PASSWORD", "")
    spec = merged.get(
        "SLAI_NODES",
        "Tokyo2SLAI:47.74.24.121:6000,Shenzhen2SLAI:47.106.11.53:6000,HongKong2SLAI:8.217.48.122:6000",
    )
    ident = Path(merged.get("SCHOOL_IDENTITY", str(Path.home() / ".ssh" / "id_ed25519")))
    return Config(
        school_host=merged.get("SCHOOL_HOST", "10.1.30.201"),
        ssh_port=int(merged.get("SCHOOL_SSH_PORT", "31222")),
        ssh_user=merged.get("SCHOOL_SSH_USER", "demo2"),
        clash_port=int(merged.get("CLASH_MIXED_PORT", "7897")),
        slai_user=user,
        slai_password=password,
        nodes=parse_nodes(spec, user, password),
        identity=ident,
        alias=merged.get("SCHOOL_ALIAS", "school-demo2"),
    )
