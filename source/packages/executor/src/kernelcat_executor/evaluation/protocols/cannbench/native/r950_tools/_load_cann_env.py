"""Load KernelCat/cann.env into os.environ. Never print secret values."""
from __future__ import annotations

import os
from pathlib import Path

ENV = Path(__file__).resolve().parents[2] / "cann.env"


def load() -> list[str]:
    loaded: list[str] = []
    for raw in ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        os.environ[key] = val
        loaded.append(key)
    os.environ.setdefault("CANN_BOARD", "910c")
    os.environ["CANN_BOARD"] = "910c"
    os.environ.setdefault(
        "PUSH_SKIP_ACCTS",
        ",".join(["GITCODE_TOKEN_%d" % i for i in range(6, 20)]),
    )
    return loaded


if __name__ == "__main__":
    keys = load()
    print("LOADED", len(keys), "CANN_BOARD", os.environ.get("CANN_BOARD"))
    print("HAS_TOKEN", bool(os.environ.get("GITCODE_TOKEN")))
    print("SKIP", os.environ.get("PUSH_SKIP_ACCTS"))
