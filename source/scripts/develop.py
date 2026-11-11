"""Bind this checkout to frozen Python wheels using already installed dependencies.

Run with a Python environment containing requirements.lock and an npm-ci agent
tree. No downloads, global installation, or changes to the selected interpreter.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from python_deployment import prepare_deployment


def main():
    bundle = Path(__file__).resolve().parents[2]
    for line in (bundle / "requirements.lock").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, expected = line.split("==")
        if importlib.metadata.version(name) != expected:
            raise ValueError(f"{name} must match requirements.lock ({expected})")
    node = shutil.which("node")
    if node is None:
        raise ValueError("Node 22.19+ is required")
    subprocess.run([node, "--input-type=module", "-e", "await import('@earendil-works/pi-coding-agent')"],
                   cwd=bundle / "source/apps/agent", check=True)
    root = bundle / ".development"
    root.mkdir(mode=0o700, exist_ok=True)
    deployment = prepare_deployment(bundle / "source", root / ("build-" + uuid.uuid4().hex))
    values = {"python": sys.executable, "node": node, "pythonpath": str(deployment.import_root),
              "expected": str(deployment.binding_path)}
    for key, value in values.items():
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("invalid development runtime path")
        temporary = root / (key + ".tmp")
        temporary.write_text(value + "\n")
        os.replace(temporary, root / key)
    (root / "ready").write_text("ready\n")
    print(json.dumps({"launcher": str(bundle / "kernelagent"), **values}))


if __name__ == "__main__":
    main()
