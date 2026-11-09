"""Offline authored fixture for packaged source collection and reopened-state replay."""
from __future__ import annotations

from pathlib import Path

from kernelcat_executor.contracts.models import Json, canonical

from kernelcat_ingestion.pipeline import external_state
from kernelcat_ingestion.plans import Asset, SourcePlan, SourceSpec
from kernelcat_ingestion.ports import IngestionCommands
from kernelcat_ingestion.snapshots import DataError, install, sha256


def _fixture(root: Path) -> SourcePlan:
    content = {
        "before.py": b"def kernel(x):\n    return x + 0\n",
        "after.py": b"def kernel(x):\n    return x\n",
        "notes.md": b"# Authored fixture\nPreserve masked tails; no performance was measured.\n",
        "shape.json": b'{"dtype":"float16","tail":true}',
        "timings.csv": b"trial,latency_us\n1,10\n2,10\n",
    }
    roles = ("before", "after", "document", "config", "measurement")
    assets = []
    for (name, raw), role in zip(content.items(), roles, strict=True):
        install(root / "inputs" / name, raw)
        assets.append(Asset.model_validate({"path": name, "role": role, "sha256": sha256(raw)}))
    source = SourceSpec(
        key="fixture", kind="local", location=str(root / "inputs"), revision="authored-v1",
        source_class="community_implementation", assets=tuple(assets),
        approval_ref="authored-offline-fixture", fixture=True,
    )
    return SourcePlan(name="offline", sources=(source,), requests=0)


def smoke(output: Path, *, commands: IngestionCommands) -> Json:
    root = external_state(output)
    if root.exists():
        raise DataError("smoke output must be a new directory")
    plan = _fixture(root)
    install(root / "plan.json", canonical(plan).encode())
    commands.initialize(root / "wiki")
    report = commands.collect(root / "campaign", root / "wiki", plan, authorize_network=False)
    again = commands.collect(root / "campaign", root / "wiki", plan, authorize_network=False)
    if report["status"] != "completed" or again != report:
        raise DataError("smoke collection or reopened-state replay did not complete")
    return {
        "status": "passed", "fixture": True, "report": report,
        "replay": "same_receipts_and_report", "model_called": False,
        "device_called": False, "network_called": False,
    }
