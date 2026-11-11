"""Rebuildable product exports. Only the original Journal authorizes effects."""
import json
import os
from pathlib import Path
import uuid

from kernelcat_executor.contracts.models import canonical


def replace_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def export_application(host):
    state = host.status()
    output = Path(host.descriptor["output"])
    journal = host.owner.gateway.store
    events = journal.events()
    bindings = [item for item in state["configurations"] if item.get("operation") == "runtime_manifest"]
    manifest = {"schema_version": "kernelbridge.application-manifest/v1", "application_session_id": host.identity,
        "journal": str(journal.path), "native_sessions": str(journal.path.parent / "pi-sessions"),
        "journal_cursor": len(events) - 1, "exports_are_projections": True,
        "task_sha256": host.descriptor["task_sha256"], "config_sha256": host.descriptor["config_sha256"],
        "evaluator_sha256": host.evaluator["sha256"], "runtime_bindings": bindings,
        "measurement_profile": host.task.contract.get("measurement_profile"),
        "device_qualification": "pending"}
    outputs = {"manifest.json": manifest, "loopx/state.json": state,
        "state/host/projection.json": {"journal_cursor": state["cursor"], "application_session_id": host.identity},
        "result.json": {"status": (state["goal"] or {}).get("status", "session"), "task_version": state["task_version"],
                        "source": state.get("checkpoint"), "budget": state["budget"], "pending_effect_ids": state["pending_effect_ids"]},
        "acceptance.json": {"single_best": state["single_best"], "best_accepted": state["best"], "platform": state["platform"],
                            "evidence": state["evidence"], "device_qualification": "pending"}}
    for name, value in outputs.items():
        replace_file(output / name, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    replace_file(output / "events.jsonl", "".join(canonical(item) + "\n" for item in journal._events(events)))
    return manifest
