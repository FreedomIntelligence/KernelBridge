#!/usr/bin/env python3
"""Paramiko transport for probe runners (OpenSSH stdout-hang workaround).

Usage:
  school_xfer.py put <local> <remote> [timeout=300]
  school_xfer.py exec-stdin <timeout> <remote argv...>   # remote script read from stdin
  school_xfer.py exec <timeout> <remote cmd...>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from school_sdk.ssh import ssh_exec  # noqa: E402


def _emit(proc) -> int:
    out = proc.stdout if isinstance(proc.stdout, (bytes, bytearray)) else (proc.stdout or "").encode()
    err = proc.stderr if isinstance(proc.stderr, (bytes, bytearray)) else (proc.stderr or "").encode()
    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    return int(proc.returncode or 0)


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[1] == "put":
        data = Path(argv[2]).read_bytes()
        remote = argv[3]
        if remote.startswith("remote="):
            remote = remote[len("remote="):]
        timeout = int(argv[4]) if len(argv) > 4 else 300
        return _emit(ssh_exec(f"cat > {remote} && echo PUT_OK", timeout=timeout, stdin=data))
    if len(argv) >= 4 and argv[1] in ("exec", "exec-stdin"):
        timeout = int(argv[2])
        cmd = " ".join(argv[3:])
        data = sys.stdin.buffer.read() if argv[1] == "exec-stdin" else None
        return _emit(ssh_exec(cmd, timeout=timeout, stdin=data))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
