"""Internal native worker: consumes a frozen request; never used to provision hardware."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from kernelcat_executor.build.inputs import BuildSettings
from kernelcat_executor.evaluation.protocols.six_source.logging import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    job = args.request.resolve().parent
    record = json.loads(args.request.read_text())
    request = record["request"]
    frozen = {}
    for key in ("candidate", "reference"):
        content = (job / (key + ".py")).read_bytes()
        if hashlib.sha256(content).hexdigest() != record[key + "_sha256"]:
            raise ValueError(f"frozen {key} changed")
        frozen[key] = content.decode("utf-8")
    setup_logging()
    # Heavy native imports happen after all input verification, never for --help.
    from kernelcat_executor.evaluation.protocols.six_source.session import AscendBackend
    from kernelcat_executor.evaluation.protocols.six_source.evaluator import evaluate
    settings = BuildSettings(request["operator"], job / "build", request["soc"], request["device"])
    result = evaluate(frozen["candidate"], frozen["reference"], AscendBackend(settings), fast=request["fast"])
    temporary = job / "native-result.tmp"
    temporary.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
    temporary.replace(job / "native-result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
