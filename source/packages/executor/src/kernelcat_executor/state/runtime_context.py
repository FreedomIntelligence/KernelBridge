"""Joint context/input adoption, called only inside the owner admission transaction."""
from __future__ import annotations

from typing import cast

from kernelcat_executor.contracts.context import validate_context_value, validate_submission
from kernelcat_executor.contracts.json_decoder import decode_object
from kernelcat_executor.contracts.models import Conflict, Json, Policy, canonical
from kernelcat_executor.contracts.runtime import ContextAdmission, bytes_digest, require
from kernelcat_executor.state.runtime_state import check_expected, clone, retain


def context_event(state: Json, record: Json, adoption: ContextAdmission) -> Json:
    commit = cast(Json, adoption.commit)
    expected = commit["expected"]
    require(expected["run_id"], record["run"]["run_id"], "input Run")
    require(expected["trial_id"], record["trial"]["trial_id"], "input Trial")
    check_expected(state, expected)
    policy = Policy.model_validate_json(canonical(record["policy"]))
    require(adoption.source["runtime"], expected, "context source execution stamp")
    validate_submission(dict(adoption.source), dict(adoption.context), policy)
    require(adoption.context["budget"], {key: state[key] for key in ("limit", "spent", "reserved")},
            "current input credit budget")
    raw = commit["input_json"].encode("utf-8", errors="strict")
    actual = decode_object(raw)
    checked_input = validate_context_value(actual, "runtime input")
    require(checked_input, adoption.actual_input, "actual selected input bytes")
    manifest, turn = commit["manifest"], commit["turn"]
    input_digest = bytes_digest(raw)
    _input_fields(manifest, turn, record, expected, input_digest, len(raw), policy.context_bytes)
    reference = _input_reference(state, record, manifest["input_ref_id"], input_digest, len(raw))
    capsule = _capsule(commit, record, manifest)
    _references(manifest, record, reference)
    trial = {**record["trial"], "revision": record["trial"]["revision"] + 1}
    branch = {**record["branch"], "revision": record["branch"]["revision"] + 1,
              "context_revision": manifest["revision"]}
    session = {**record["session"], "revision": record["session"]["revision"] + 1,
        "active_trial": manifest["trial"],
        "active_context": {"context_id": manifest["context_id"], "revision": manifest["revision"]}}
    objects = [trial, branch, session, manifest, turn]
    if "capsule" not in record:
        objects.append(capsule)
    for run in state["runtime_runs"].values():
        if run["run"]["run_id"] == record["run"]["run_id"]:
            continue
        known = {(value["kind"], value.get("context_id", value.get("turn_context_id", value.get("capsule_id"))))
                 for value in run["graph"]["objects"]}
        for value in (manifest, turn, capsule):
            key = (value["kind"], value.get("context_id", value.get("turn_context_id", value.get("capsule_id"))))
            if key in known:
                raise Conflict("context object identity belongs to another Run")
    retain(clone(record["graph"]), objects)
    return {"kind": "runtime_context", "run_id": record["run"]["run_id"],
        "changes": {"trial": trial, "branch": branch, "session": session,
                    "context": manifest, "capsule": capsule},
        "objects": objects, "references": [reference], "input_json": commit["input_json"]}


def _input_fields(manifest: Json, turn: Json, record: Json, expected: Json,
                  input_digest: str, size: int, limit: int) -> None:
    if size > limit:
        raise Conflict("actual selected input bytes exceed the current context limit")
    trial_ref = {"trial_id": record["trial"]["trial_id"], "revision": record["trial"]["revision"]}
    for value in (manifest, turn):
        require(value["trial"], trial_ref, "context Trial revision")
        require(value["input_digest"], input_digest, "context input digest")
        require(value["input_ref_id"], manifest["input_ref_id"], "context input reference")
        require(value["policy_digest"], record["run"]["policy_digest"], "context policy")
    require(manifest["revision"], expected["context_revision"] + 1, "next context revision")
    require(manifest["input_bytes"], size, "actual input byte count")
    require(manifest["limit_bytes"], limit, "current context byte ceiling")
    require(manifest["compiler_version"], record["compiler_version"], "compiler observation")
    require(manifest["prune_policy_version"], record["version_lock"]["lock"]["rules"]["prune_policy"],
            "prune policy observation")
    next_session = {**expected["session"], "revision": expected["session"]["revision"] + 1}
    require(turn["agent_session"], next_session, "proposed context Session revision")
    for key in ("working_program_id", "trial_feedback_cursor", "proposal_revision", "external_snapshot"):
        require(turn[key], record["trial"][key], "Turn " + key)
    require(turn["context"], {"context_id": manifest["context_id"], "revision": manifest["revision"]},
            "Turn ContextManifest")
    require(turn["allowed_actions"], sorted(set(turn["allowed_actions"])), "stable unique allowed_actions")
    if not set(turn["allowed_actions"]).issubset(record["tool_names"]):
        raise Conflict("TurnContext proposes an unavailable action")


def _input_reference(state: Json, record: Json, ref_id: str, checksum: str, size: int) -> Json:
    if any(reference["ref_id"] == ref_id for run in state["runtime_runs"].values()
           for reference in run["references"]):
        raise Conflict("journal input reference identity already exists")
    return {"ref_id": ref_id, "owner": "journal", "record_id": ref_id, "digest": checksum,
        "digest_scope": "exact UTF-8 selected model input",
        "locator": "journal:run/" + record["run"]["run_id"] + "/trial/" + record["trial"]["trial_id"]
                   + "/input/" + ref_id, "byte_length": size}


def _capsule(commit: Json, record: Json, manifest: Json) -> Json:
    if "capsule" in record:
        if "capsule" in commit:
            raise Conflict("initial Capsule may be adopted only once")
        require(manifest["capsule_id"], record["capsule"]["capsule_id"], "retained Capsule")
        return dict(record["capsule"])
    capsule = commit.get("capsule")
    if capsule is None:
        raise Conflict("first ContextCommit requires its initial Capsule")
    trial = record["trial"]
    require(capsule, {"kind": "capsule", "capsule_id": manifest["capsule_id"],
        "initial_trial": {"trial_id": trial["trial_id"], "revision": trial["revision"]},
        "parent_program_id": trial["parent_program_id"],
        "proposal_revision": trial["proposal_revision"], "external_snapshot": trial["external_snapshot"],
        "compiler_version": record["compiler_version"], "policy_digest": trial["policy_digest"],
        "input_ref_id": manifest["input_ref_id"], "input_digest": manifest["input_digest"],
        "initial_context": {"context_id": manifest["context_id"], "revision": manifest["revision"]}},
        "initial Capsule")
    return dict(capsule)


def _references(manifest: Json, record: Json, new_reference: Json) -> None:
    known = {value["ref_id"]: value for value in record["references"]}
    known[new_reference["ref_id"]] = new_reference
    dependencies = manifest["dependency_ref_ids"]
    if len(set(dependencies)) != len(dependencies) or any(ref not in known for ref in dependencies):
        raise Conflict("context dependencies must be unique retained owner references")
    if not set(record["required_ref_ids"]).issubset(dependencies):
        raise Conflict("context omitted a protected source/parent/policy/parameter reference")
    items = manifest["items"]
    if len({item["source_ref_id"] for item in items}) != len(items):
        raise Conflict("duplicate context reference selections")
    for item in items:
        if item["source_ref_id"] not in dependencies:
            raise Conflict("context selection has no pinned dependency")
        if "selected_digest" in item:
            require(item["selected_digest"], known[item["source_ref_id"]]["digest"],
                    "selected owner reference digest")
