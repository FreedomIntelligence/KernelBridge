"""OpenSSH ProxyCommand entry: python -m school_sdk.socks_cmd %h %p"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `python path/to/socks_cmd.py %h %p` without -m
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from school_sdk.config import load_config
    from school_sdk.socks import connect_first, relay_stdio
else:
    from .config import load_config
    from .socks import connect_first, relay_stdio


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: socks_cmd.py host port", file=sys.stderr)
        return 2
    dest_host, dest_port = argv[0], int(argv[1])
    cfg = load_config()
    if not cfg.slai_password:
        print("SLAI_PASSWORD missing: copy school.env.example → school.env", file=sys.stderr)
        return 2
    if not cfg.nodes:
        print("no SLAI_NODES configured", file=sys.stderr)
        return 2
    _node, sock = connect_first(cfg.nodes, dest_host, dest_port)
    relay_stdio(sock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
