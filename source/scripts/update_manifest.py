"""Refresh PIN_MANIFEST.json after a source change.

Updates size/SHA256 of every listed file, adds new files under source/ and
examples/ (the inventories install.py verifies exactly), drops deleted ones and
adds any extra repository files named on the command line (docs, validation).

    python3 source/scripts/update_manifest.py [EXTRA_PATH ...]
"""
from __future__ import annotations

import hashlib
import fcntl
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIPPED_DIRECTORIES = {"__pycache__"}
RELEASE_ROOT_FILES = {
    ".gitignore", "README.md", "install.py", "kernelagent", "requirements.lock",
    "restore.py", "restore.sh",
}


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


def refresh(extras: list[str], *, release: bool = False, source_commit: str | None = None) -> int:
    path = ROOT / "PIN_MANIFEST.json"
    manifest = ({
        "schema_version": "kernelcat.customer-source/v1",
        "installation": "required-online",
        "resource_selection": "source/runtime-resources.json",
        "files": {},
    } if release else json.loads(path.read_text()))
    if source_commit is not None:
        manifest["source"] = {"commit": source_commit}
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
    roots = RELEASE_ROOT_FILES if release else set()
    for relative in sorted(present | roots | set(extras)):
        if relative not in files:
            files[relative] = entry(relative)
            changes.append("+ " + relative)
    manifest["files"] = dict(sorted(files.items()))
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w") as stream:
            stream.write(json.dumps(manifest, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print("\n".join(changes) or "PIN_MANIFEST.json is current")
    return 0


def main(extras: list[str], *, release: bool = False, source_commit: str | None = None) -> int:
    # Serialize the entire read/scan/replace operation. Atomic replacement alone
    # lets an earlier scan overwrite a newer writer's manifest with stale hashes.
    if release:
        # Lock the directory without creating development state in a release.
        descriptor = os.open(ROOT, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return refresh(extras, release=True, source_commit=source_commit)
        finally:
            os.close(descriptor)
    state = ROOT / '.development'
    state.mkdir(exist_ok=True)
    with (state / 'manifest.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return refresh(extras, source_commit=source_commit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="refresh a separate source bundle")
    parser.add_argument("--release", action="store_true", help="rebuild publication metadata without development history")
    parser.add_argument("--source-commit", help="record the exact source commit")
    parser.add_argument("extras", nargs="*")
    args = parser.parse_args()
    if args.root is not None:
        ROOT = args.root.resolve(strict=True)
    sys.exit(main(args.extras, release=args.release, source_commit=args.source_commit))
