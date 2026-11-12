"""Project acceptance exclusively from settled, evaluator-bound receipts."""
import hashlib
import json
from pathlib import Path
from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.models import Conflict
from kernelcat_worker.application_artifacts import read_regular, verify_evaluator
from kernelcat_worker.candidate_retention import field


def collect_acceptance(host):
    state = host.journal.state()
    declared = state["task"].get("contract", {}).get("acceptance", {})
    if declared.get('kind') == 'operator_pipeline':
        from kernelcat_worker.application_pipeline_acceptance import collect_pipeline_acceptance
        return collect_pipeline_acceptance(host, state, declared)
    retention = host.config.candidate_retention
    if retention:
        tool, schema = retention.tool, retention.outcome_schema
        source_fields = retention.source_hashes
    elif declared.get("tool") and declared.get("outcome_schema") and declared.get("source_hashes"):
        tool, schema, source_fields = declared["tool"], declared["outcome_schema"], declared["source_hashes"]
    else:
        return
    verify_evaluator(host.evaluator)
    contexts = {row["action"]["id"]: row.get("application_context", {})
                for row in host.owner.gateway.store._events(host.owner.gateway.store.events()) if row["kind"] == "admitted"}
    validator = Draft202012Validator(schema)
    known = {item["evidence_ref"] for item in state["evidence"]}
    for identity, action in host.owner.gateway.store.snapshot()["actions"].items():
        outcome = action.get("outcome", {})
        if (identity in known or action["action"]["tool"] != tool or not action.get("terminal")
                or outcome.get("status") != "completed" or not validator.is_valid(outcome)):
            continue
        context = contexts.get(identity, {})
        if context.get("task_version") != state["task_version"] or context.get("evaluator_sha256") != host.evaluator["sha256"]:
            continue
        snapshot = json.loads(read_regular(host.owner.gateway.store.path.parent / "checkpoints" / (context["source_sha256"] + ".json")))
        live = {item["path"]: item["sha256"] for item in snapshot["files"]}
        if not all(live.get(path) == field(outcome, key) for path, key in source_fields.items()):
            continue
        measured = outcome.get("data", {}).get("acceptance_evidence", {})
        # A legacy passing evaluation establishes correctness only. Timing and
        # paired promotion need explicit evaluator metadata, never inferred scores.
        tier = measured.get("evidence_tier", "E1")
        receipt = {**measured, "evidence_tier": tier, "source_sha256": snapshot["source_sha256"],
            "evaluator_sha256": host.evaluator["sha256"], "task_version": state["task_version"],
            "measurement_profile_version": host.task.contract.get("measurement_profile", {}).get("version", host.evaluator["sha256"]),
            "evidence_ref": identity, "correct": True, "full_suite": True,
            "source_files": live}
        try:
            host.journal.command("evidence", receipt)
        except Conflict:
            continue  # Invalid receipt remains visible in the execution Journal.
        required = declared.get("evidence_tier")
        score = receipt.get("score")
        target = declared.get("target")
        qualifies = required is not None and tier == required
        if target is not None:
            qualifies = qualifies and isinstance(score, (float, int)) and score >= target
        if qualifies and host.config.application.complete_on_acceptance and state["goal"] and state["goal"]["status"] == "active":
            host.journal.command("goal_outcome", {"status": "completed", "evidence_ref": identity})
