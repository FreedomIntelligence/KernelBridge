"""Artifact preparation for finite Trial commands, outside Journal transactions."""
from __future__ import annotations

from typing import TYPE_CHECKING

from kernelcat_executor.contracts.models import Conflict, Json, Task
from kernelcat_executor.contracts.runtime import require

if TYPE_CHECKING:
    from kernelcat_executor.operations.runtime import RuntimeOwner


def prepare_trial(owner: RuntimeOwner, request: Json) -> Json:
    previous = owner.journal.preparation(request["expected"]["run_id"], request["expected"]["trial_id"])
    owner.verify_configuration(previous)
    admitted = owner.journal.prepare_trial(request)
    existing = admitted.get("existing_preparation")
    if existing is not None:
        owner.verify_preparation(existing)
        return owner.journal.prepared_trial(request["intent_id"])
    state = owner.journal.snapshot()
    current = state["runtime_runs"][admitted["run_id"]]
    references = current["references"]
    parent = admitted["parent_program"]
    parent_ref = next(value for value in references if value["ref_id"] == parent["manifest_ref_id"])
    bundle = owner.artifacts.program(parent_ref)
    parameter = owner.artifacts.put(request["proposal"]["parameter_source"].encode("utf-8"), "parameters")
    known = {value["ref_id"]: value for value in references}
    if parameter["ref_id"] in known:
        require(parameter, known[parameter["ref_id"]], "retained parameter artifact")
    known[parameter["ref_id"]] = parameter
    required = [value["ref_id"] for value in references
                if value["ref_id"].startswith(("task-contract/", "policy/", "version-lock/"))]
    required.extend([parent_ref["ref_id"], parameter["ref_id"], *admitted["memory"]["source_ref_ids"]])
    required = list(dict.fromkeys(required))
    if len(required) > 1024:
        raise Conflict("next Trial protected context reference capacity reached")
    old_task = previous["task"]
    task = Task(id=admitted["host_task_id"], goal=old_task["goal"], scope=old_task["scope"],
        contract=old_task["contract"], seed_files={entry["path"]: entry["text"] for entry in bundle["files"]})
    prepared = {key: admitted[key] for key in ("request", "request_digest", "transition_stamp",
        "host_task_id", "parent_program", "memory", "workspace_resource_id", "workspace_lease_deadline",
        "branch_resource_id", "branch_lease_deadline")}
    prepared.update(schema_version="kernelcat.prepared-trial/v1", references=list(known.values()),
                    parameter_ref_id=parameter["ref_id"], required_ref_ids=required)
    record = {key: previous[key] for key in ("source", "manifest", "policy", "profile",
                                            "lease_seconds", "tool_names", "version_lock")}
    record.update(prepared=prepared, task=task.model_dump(mode="json"), required_ref_ids=required)
    # The admitted deadlines and transition stamp survive crashes and repeated reads.
    owner.journal.finish_trial_preparation(record)
    return owner.journal.prepared_trial(request["intent_id"])
