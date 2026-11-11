"""Refresh PIN_MANIFEST.json after a source change.

Updates size/SHA256 of every listed file, adds new files under source/ and
examples/ (the inventories install.py verifies exactly), drops deleted ones and
adds any extra repository files named on the command line (docs, validation).

    python3 source/scripts/update_manifest.py [EXTRA_PATH ...]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIPPED_DIRECTORIES = {"__pycache__"}


def entry(relative: str) -> dict:
    data = (ROOT / relative).read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def inventory() -> set[str]:
    found = set()
    for top in ("source", "examples"):
        for directory, names, files in os.walk(ROOT / top):
            if Path(directory) == ROOT / "source/apps/agent":
                names[:] = [name for name in names if name != "node_modules"]
            names[:] = [name for name in names if name not in SKIPPED_DIRECTORIES]
            found.update((Path(directory) / name).relative_to(ROOT).as_posix()
                         for name in files if name != ".DS_Store" and not name.endswith(".pyc"))
    return found


def main(extras: list[str]) -> int:
    path = ROOT / "PIN_MANIFEST.json"
    manifest = json.loads(path.read_text())
    files = manifest["files"]
    present = inventory()
    changes = []
    for relative in list(files):
        if (relative.startswith(("source/", "examples/")) and relative not in present) or not (ROOT / relative).is_file():
            del files[relative]
            changes.append("- " + relative)
        elif files[relative] != entry(relative):
            files[relative] = entry(relative)
            changes.append("~ " + relative)
    for relative in sorted(present | set(extras)):
        if relative not in files:
            files[relative] = entry(relative)
            changes.append("+ " + relative)
    manifest["files"] = dict(sorted(files.items()))
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print("\n".join(changes) or "PIN_MANIFEST.json is current")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
