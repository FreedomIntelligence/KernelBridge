"""Load an explicitly selected CANN environment file. Never print secret values."""
from __future__ import annotations

import os
from pathlib import Path

ENV = Path(os.environ.get("CANN_ENV_FILE", str(Path.home() / ".config/kernelbridge/cann.env")))


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
    return loaded


if __name__ == "__main__":
    keys = load()
    print("LOADED", len(keys), "CANN_BOARD", os.environ.get("CANN_BOARD"))
    print("HAS_TOKEN", bool(os.environ.get("GITCODE_TOKEN")))
    print("SKIP", os.environ.get("PUSH_SKIP_ACCTS"))
