"""Deterministic local acceptance fixture, never a device/performance claim."""
import hashlib
import json
from pathlib import Path
import sys
import time

action = json.load(sys.stdin)
if len(sys.argv) == 2:
    gate = Path(sys.argv[1].removeprefix('--gate='))
    with open(gate, 'a') as stream:
        stream.write('accepted\n')
        stream.flush()
    deadline = time.monotonic() + 30
    while not gate.with_suffix('.release').exists():
        if time.monotonic() >= deadline: raise TimeoutError('fixture gate not released')
        time.sleep(.05)
source = Path("solution.py").read_bytes()
passed = source == b"x = 2\n"
print(json.dumps({"status": "completed" if passed else "failed", "data": {
    "candidate_sha256": hashlib.sha256(source).hexdigest(), "correct": passed, "fixture_only": True}}))
