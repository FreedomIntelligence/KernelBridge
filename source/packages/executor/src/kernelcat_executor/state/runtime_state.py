"""Pure managed projections and fences used inside the original Journal lock.

Replay is O(E + retained graph bytes), like the original JSONL owner. Selected
RuntimeState returns one Run graph; it never scans a second ledger or a cache.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import cast

from kernelcat_executor.contracts.knowledge_fences import (
    CAPTURE_INSTALLED_EVENT,
    CAPTURE_PREPARED_EVENT,
    FENCE_CONTROL_EVENT,
    FENCE_EVENT,
    TRIAL_CONTROL_EVENT,
    TRIAL_FROZEN_EVENT,
    TRIAL_PREPARED_EVENT,
)
from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, canonical
from kernelcat_executor.generated.runtime import RuntimeState
from kernelcat_executor.state.journal import Journal
from kernelcat_executor.state.runtime_knowledge import apply_fence, check_trial_fence, trial_key

IDENTITY_FIELDS = {"task_run": "run_id", "trial": "trial_id", "program": "program_id",
    "branch": "branch_id", "agent_session": "agent_session_id", "lease": "lease_id",
    "reservation": "reservation_id", "proposal_revision": "proposal_id",
    "task_memory_revision": "memory_id", "capsule": "capsule_id",
    "context_manifest": "context_id", "turn_context": "turn_context_id"}


def clone(value: Json) -> Json:
    return cast(Json, json.loads(canonical(value)))


def budget(state: Json) -> Json:
    return {key: state[key] for key in ("limit", "spent", "reserved")}


def object_key(value: Json) -> tuple[str, str, int]:
    return value["kind"], value[IDENTITY_FIELDS[value["kind"]]], value.get("revision", 0)


def retain(graph: Json, values: list[Json]) -> None:
    indexed = {object_key(value): value for value in graph["objects"]}
    for value in values:
        key = object_key(value)
        previous = indexed.get(key)
        if previous is not None and previous != value:
            raise Conflict("immutable runtime object revision was reused")
        indexed[key] = value
    graph["objects"] = list(indexed.values())
    if len(graph["objects"]) > 16384:
        raise Conflict("selected runtime graph capacity reached")


def _register_task(state: Json, record: Json) -> None:
    task = record["task"]
    state["tasks"][task["id"]] = task
    state["agent_profiles"].setdefault(task["scope"], record["profile"])
    state["agent_initial_profiles"].setdefault(task["scope"], record["profile"]["profile"])
    state["agent_pins"].setdefault(task["id"], record["profile"])


def _retain_index(indexed: dict[tuple[str, str, int], Json], values: list[Json]) -> None:
    for value in values:
        key = object_key(value)
        if key in indexed and indexed[key] != value:
            raise Conflict("immutable runtime object revision was reused")
        indexed[key] = value
    if len(indexed) > 16384:
        raise Conflict("selected runtime graph capacity reached")


def _install_capture(state: Json, event: Json) -> None:
    request = event["request"]
    identity = request["preparation_intent_id"]
    state["runtime_capture_installs"][identity] = event
    preparation = state["runtime_preparation_index"][trial_key(event["run_id"], event["trial_id"])]
    observed = preparation["prepared"]
    observed["knowledge"] = event["binding"]
    observed["references"].append(event["reference"])
    preparation["required_ref_ids"].append(event["reference"]["ref_id"])
    observed["required_ref_ids"] = preparation["required_ref_ids"]


def apply_runtime(state: Json, rows: list[Json]) -> Json:
    state.update(runtime_preparations={}, runtime_prepared={}, runtime_runs={}, runtime_intents={},
                 runtime_trials={}, runtime_trial_admissions={}, runtime_trial_prepared={},
                 runtime_pending_trials={}, runtime_preparation_index={}, runtime_run_stops={},
                 runtime_capture_requests={}, runtime_capture_installs={})
    state.update(runtime_knowledge_fences={}, runtime_knowledge_trials={}, runtime_knowledge_consumers={},
                 runtime_knowledge_captures={}, runtime_knowledge_retractions={})
    state["account"] = rows[0]["account"]
    state["account_revision"] = len(rows) - 1
    graph_indices: dict[str, dict[tuple[str, str, int], Json]] = {}
    for event in Journal._events(rows[1:]):
        kind = event["kind"]
        if kind == "runtime_prepare_intent":
            state["runtime_preparations"][event["intent_id"]] = event
        elif kind == "runtime_prepared":
            prepared = clone(event)
            state["runtime_prepared"][event["intent_id"]] = prepared
            launch = prepared["manifest"]
            state["runtime_preparation_index"][trial_key(launch["run_id"], launch["trial_id"])] = prepared
        elif kind == "runtime_frozen":
            record = clone(event["record"])
            run_id, trial_id = record["run"]["run_id"], record["trial"]["trial_id"]
            state["runtime_runs"][run_id] = record
            state["runtime_trials"][run_id] = {trial_id: record}
            graph_indices[run_id] = {object_key(value): value for value in record["graph"]["objects"]}
            _register_task(state, record)
        elif kind in {"runtime_context", "runtime_control", "runtime_candidate", "runtime_finalized",
                      FENCE_CONTROL_EVENT, TRIAL_CONTROL_EVENT}:
            record = state["runtime_runs"][event["run_id"]]
            record.update(clone(event["changes"]))
            if kind == TRIAL_CONTROL_EVENT:
                record.pop("last_control_request", None)
            _retain_index(graph_indices[event["run_id"]], event["objects"])
            record["references"].extend(event.get("references", []))
            if kind == "runtime_control" and "run_id" in event["request"]:
                state["runtime_run_stops"][event["run_id"]] = True
            if kind == TRIAL_CONTROL_EVENT:
                request = event["request"]
                state["runtime_trial_admissions"][request["intent_id"]] = clone(event["preparation"])
                state["runtime_pending_trials"][event["run_id"]] = request["intent_id"]
        elif kind == TRIAL_PREPARED_EVENT:
            prepared = clone(event["record"])
            state["runtime_trial_prepared"][event["intent_id"]] = prepared
            request = prepared["prepared"]["request"]
            key = trial_key(request["expected"]["run_id"], request["trial_id"])
            state["runtime_preparation_index"][key] = prepared
        elif kind == TRIAL_FROZEN_EVENT:
            old = state["runtime_runs"][event["run_id"]]
            record = {**clone(event["record"]), "graph": old["graph"],
                      "references": old["references"], "run_budget": old["run_budget"]}
            record["references"].extend(event["references"])
            state["runtime_runs"][event["run_id"]] = record
            state["runtime_trials"][event["run_id"]][record["trial"]["trial_id"]] = record
            state["runtime_pending_trials"].pop(event["run_id"], None)
            _retain_index(graph_indices[event["run_id"]], event["objects"])
            _register_task(state, record)
        elif kind == CAPTURE_PREPARED_EVENT:
            state["runtime_capture_requests"][trial_key(event["run_id"], event["trial_id"])] = clone(event["request"])
        elif kind == CAPTURE_INSTALLED_EVENT:
            _install_capture(state, event)
        elif kind == "runtime_generation_stopped":
            state["runtime_runs"][event["run_id"]]["status"] = "generation_stopped"
        elif kind == FENCE_EVENT:
            apply_fence(state, event, rows[event["seq"]])
        if kind.startswith("runtime_") and "request" in event:
            identity = event["request"].get("intent_id")
            if identity is not None:
                state["runtime_intents"][kind + "/" + identity] = event
        if kind not in {"admitted", "settled", "cancelled_unstarted"}:
            continue
        attempt = event if kind == "admitted" else state["actions"][event["action_id"]]
        binding = attempt.get("binding")
        if binding is not None:
            run_id, trial_id = binding["run_id"], binding["trial_id"]
            record = state["runtime_trials"][run_id][trial_id]
            for name in ("run_budget", "trial_budget"):
                Journal._credit_event(record[name], event, attempt)
            if state["account"].get("multi_trial") and kind != "admitted":
                trial = {**record["trial"], "revision": record["trial"]["revision"] + 1,
                         "trial_feedback_cursor": event["seq"]}
                record["trial"] = trial
                _retain_index(graph_indices[run_id], [trial])
    for run_id, indexed in graph_indices.items():
        graph = state["runtime_runs"][run_id]["graph"]
        graph["objects"] = list(indexed.values())
        graph["owner_references"] = state["runtime_runs"][run_id]["references"]
    return state


def selected_record(state: Json, run_id: str, trial_id: str) -> Json:
    record = state["runtime_trials"].get(run_id, {}).get(trial_id)
    if record is None:
        raise Conflict("unknown frozen Run/Trial")
    return cast(Json, record)


def pending(state: Json, run_id: str) -> list[Json]:
    return [attempt for attempt in state["actions"].values()
            if attempt.get("binding", {}).get("run_id") == run_id and not attempt.get("terminal")]


def project(state: Json, run_id: str, trial_id: str) -> RuntimeState:
    record = selected_record(state, run_id, trial_id)
    unresolved = [attempt for attempt in pending(state, run_id)
                  if attempt["binding"]["trial_id"] == trial_id]
    quarantined = any(item.get("outcome") or (item.get("started") and
        item["binding"]["generation"] != record["generation"]) for item in unresolved)
    status = "reconciling" if quarantined or (record.get("stopped") and unresolved) else record["status"]
    if record.get("stopped") and not unresolved:
        status = "stopped"
    stamp: Json = {"account_id": state["account"]["account_id"], "run_id": run_id,
        "trial_id": trial_id, "session": {"agent_session_id": record["session"]["agent_session_id"],
        "revision": record["session"]["revision"]}, "generation": record["generation"],
        "lease_epoch": record["lease"]["epoch"], "control_revision": record["control_revision"],
        "branch_revision": record["branch"]["revision"],
        "context_revision": record["branch"]["context_revision"],
        "trial_revision": record["trial"]["revision"], "run_status": status,
        "trial_execution_state": record["trial"]["execution_state"],
        "session_state": record["session"]["state"]}
    for domain in ("run", "trial"):
        for name, amount in record[domain + "_budget"].items():
            stamp[domain + "_credit_" + name] = amount
    result: Json = {"schema_version": "kernelcat.runtime-state/v1",
        "manifest_digest": record["manifest_digest"], "stamp": stamp,
        **{key: record[key] for key in ("run", "trial", "branch", "session", "parent_program",
            "compiler_version", "references", "graph", "run_budget", "trial_budget")},
        "account_budget": budget(state),
        "pending_effect_ids": [item["action"]["id"] for item in unresolved]}
    if state["account"].get("multi_trial"):
        result["required_ref_ids"] = record["required_ref_ids"]
        if run_id in state["runtime_pending_trials"]:
            result["pending_trial_intent_id"] = state["runtime_pending_trials"][run_id]
    for key in ("capsule", "context", "last_control_intent_id", "last_control_request"):
        if key in record:
            result[key] = record[key]
    snapshots = [item for item in state["actions"].values() if item.get("binding", {}).get("run_id") == run_id
                 and item["binding"]["trial_id"] == trial_id
                 and item["action"]["tool"] == "__snapshot_program__"]
    if snapshots:
        snapshot = snapshots[-1]
        status = "admitted"
        if snapshot.get("outcome"):
            status = snapshot["outcome"]["status"]
            if snapshot["outcome"]["data"].get("cancelled_unstarted"):
                status = "cancelled"
        elif snapshot.get("started"):
            status = "started"
        materialization = {"request": snapshot["action"]["arguments"],
                           "operation_id": snapshot["action"]["id"], "status": status}
        if status == "completed":
            materialization["result"] = snapshot["outcome"]["data"]
        result["materialization"] = materialization
    if "last_candidate_adoption" in record:
        result["last_candidate_adoption"] = record["last_candidate_adoption"]
    if trial_key(run_id, trial_id) in state["runtime_knowledge_trials"]:
        result["knowledge_invalidated"] = True
    return cast(RuntimeState, clone(result))


def check_actor(state: Json, actor: Json, now: datetime, *,
                materialization: bool = False, quarantine: bool = True) -> Json:
    check_trial_fence(state, actor["run_id"], actor["trial_id"])
    if state["runtime_runs"].get(actor["run_id"], {}).get("trial", {}).get("trial_id") != actor["trial_id"]:
        raise Conflict("historical runtime Trial cannot grant a current Actor")
    if state["runtime_run_stops"].get(actor["run_id"]):
        raise Conflict("runtime control: global StopRun is terminal")
    current = project(state, actor["run_id"], actor["trial_id"])
    stamp: Json = cast(Json, current["stamp"])
    if any(stamp[key] != actor[key] for key in ("generation", "lease_epoch")) or (
            stamp["session"]["agent_session_id"] != actor["session_id"]):
        raise Conflict("stale runtime Session/generation/lease epoch")
    allowed = {"active", "generation_stopped"} if materialization else {"active"}
    if quarantine and stamp["run_status"] == "reconciling":
        raise InDoubt("runtime resources are quarantined by started/UNKNOWN work")
    if stamp["run_status"] not in allowed or stamp["session_state"] != "RUNNING":
        raise Conflict("runtime control no longer permits this effect")
    record = state["runtime_runs"][actor["run_id"]]
    for name in ("lease", "branch_lease"):
        lease = record[name]
        if lease["allocation_state"] != "active" or datetime.fromisoformat(
                lease["deadline"].replace("Z", "+00:00")) <= now:
            raise Conflict(f"runtime {name} expired or is not active; no release implied")
    return cast(Json, record)


def check_expected(state: Json, expected: Json) -> Json:
    if state["runtime_runs"].get(expected["run_id"], {}).get("trial", {}).get("trial_id") != expected["trial_id"]:
        raise Conflict("stale runtime ExecutionStamp selects a historical Trial")
    current = project(state, expected["run_id"], expected["trial_id"])
    if current["stamp"] != expected:
        raise Conflict("stale runtime ExecutionStamp")
    return cast(Json, state["runtime_runs"][expected["run_id"]])


def actor_from(stamp: Json) -> Json:
    return {"run_id": stamp["run_id"], "trial_id": stamp["trial_id"],
        "session_id": stamp["session"]["agent_session_id"], "generation": stamp["generation"],
        "lease_epoch": stamp["lease_epoch"]}
