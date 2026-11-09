"""Authenticate the Controller's initial graph against retained owner observations."""
from __future__ import annotations

from typing import cast

from kernelcat_executor.contracts.models import Conflict, Json
from kernelcat_executor.contracts.objects import validate_object_graph
from kernelcat_executor.contracts.runtime import require
from kernelcat_executor.state.runtime_state import clone, object_key


def _one(objects: list[Json], kind: str) -> Json:
    selected = [value for value in objects if value["kind"] == kind]
    if len(selected) != 1:
        raise Conflict(f"frozen runtime graph requires one current {kind}")
    return selected[0]


def validate_freeze(graph_value: object, preparation: Json) -> Json:
    graph = cast(Json, validate_object_graph(graph_value))
    observed, launch = preparation["prepared"], preparation["manifest"]
    source = preparation["source"]
    require(graph["provenance"], source["contract"]["provenance"], "provenance")
    require(graph["task_contract_sources"], [source, *observed["retained_sources"]], "sources")
    require(graph["version_locks"], [observed["version_lock"],
            *observed["retained_version_locks"]], "VersionLocks")
    require(graph["owner_references"], observed["references"], "owner references")
    retained = [observed["parent_program"], observed["memory"], *observed["retained_objects"]]
    keys = {object_key(value): value for value in graph["objects"]}
    if len(keys) != len(graph["objects"]):
        raise Conflict("duplicate immutable graph object identity")
    for value in retained:
        require(keys.get(object_key(value)), value, "retained object")
    retained_keys = {object_key(value) for value in retained}
    current = [value for value in graph["objects"] if object_key(value) not in retained_keys]
    record = {name: _one(current, kind) for name, kind in (
        ("run", "task_run"), ("trial", "trial"), ("branch", "branch"),
        ("session", "agent_session"), ("proposal", "proposal_revision"))}
    leases = [value for value in current if value["kind"] == "lease"]
    if len(leases) != 2 or record["branch"]["lease_id"] == record["session"]["workspace_lease_id"]:
        raise Conflict("Branch control and Session workspace require two distinct leases")
    for name, identity in (("branch_lease", record["branch"]["lease_id"]),
                           ("lease", record["session"]["workspace_lease_id"])):
        record[name] = _one([value for value in leases if value["lease_id"] == identity], "lease")
    reservations = [value for value in current if value["kind"] == "reservation"]
    if len(current) != 9 or len(reservations) != 2:
        raise Conflict("unexpected initial runtime objects or reservations")
    _validate_domain(record, reservations, launch, observed, source)
    return {**record, "graph": graph, "references": observed["references"],
        "parent_program": observed["parent_program"], "compiler_version": observed["compiler_version"],
        "manifest_digest": observed["manifest_digest"], "generation": 1, "control_revision": 1,
        "status": "active", "run_budget": {"limit": launch["run_credit_limit"], "spent": 0, "reserved": 0},
        "trial_budget": {"limit": launch["trial_credit_limit"], "spent": 0, "reserved": 0}}


def _validate_domain(record: Json, reservations: list[Json], launch: Json,
                     observed: Json, source: Json, original_run: Json | None = None) -> None:
    run, trial, branch, session = (record[key] for key in ("run", "trial", "branch", "session"))
    proposal, lease, branch_lease = record["proposal"], record["lease"], record["branch_lease"]
    common = {"run_id": launch["run_id"], "task_contract_hash": source["source_digest"],
        "version_lock_digest": observed["version_lock"]["version_lock_digest"],
        "policy_digest": observed["policy_digest"]}
    require({key: run[key] for key in common}, common, "Run binding")
    expected_run = {"kind": "task_run", **common, "host_task_id": observed["host_task_id"],
        "task_id": source["contract"]["contract_id"],
        "root_program_id": observed["parent_program"]["program_id"],
        "kb_version": observed["kb_version"], "harness_version": observed["harness_version"],
        "environment_version": observed["environment_version"],
        "reservation_id": run["reservation_id"], "seed": launch["seed"]}
    require(run, expected_run if original_run is None else original_run, "Run")
    parent_id = observed["parent_program"]["program_id"]
    external = {"kb_version": observed["kb_version"], "harness_version": observed["harness_version"],
        "memory_revision": {"memory_id": observed["memory"]["memory_id"], "revision": observed["memory"]["revision"]},
        "memory_epoch": observed["memory"]["memory_epoch"],
        "evidence_cutoff": observed["memory"]["event_cursor"]}
    if "knowledge" in observed:
        external["knowledge"] = observed["knowledge"]
    require(trial, {"kind": "trial", **common, "trial_id": launch["trial_id"], "revision": 1,
        "branch_id": launch["branch_id"], "parent_program_id": parent_id,
        "proposal_revision": {"proposal_id": launch["proposal"]["proposal_id"], "revision": 1},
        "external_snapshot": external, "model_route": launch["model_route"],
        "reservation_id": trial["reservation_id"], "working_program_id": parent_id,
        "trial_feedback_cursor": 0, "execution_state": "GENERATING", "settlement_state": "PENDING",
        "reward_revisions": []}, "Trial")
    require(branch, {"kind": "branch", "branch_id": launch["branch_id"], "revision": 1,
        "run_id": launch["run_id"], "initial_parent_program_id": parent_id,
        "active_parent_program_id": parent_id, "state": "ACTIVE",
        "lease_id": branch_lease["lease_id"], "context_revision": 0}, "Branch")
    expected_session = {"kind": "agent_session", "agent_session_id": launch["session_id"],
        "revision": 1, "run_id": launch["run_id"], "workspace_lease_id": lease["lease_id"],
        "active_trial": {"trial_id": launch["trial_id"], "revision": 1}, "state": "RUNNING"}
    for key in ("provider_thread_id", "provider_root_id", "adapter_version"):
        if session[key]["state"] != "unknown":
            raise Conflict("fresh in-memory Session cannot fabricate provider/adapter identity")
        expected_session[key] = session[key]
    require(session, expected_session, "Session")
    _validate_lease(lease, "agent_session", launch["session_id"], observed["workspace_resource_id"],
                    observed["workspace_lease_deadline"])
    _validate_lease(branch_lease, "branch", launch["branch_id"], observed["branch_resource_id"],
                    observed["branch_lease_deadline"])
    if lease["resource_id"] == branch_lease["resource_id"]:
        raise Conflict("Branch control and workspace must reference distinct resources")
    initial_proposal = clone(launch["proposal"])
    initial_proposal.pop("parameter_source")
    require(proposal, {"kind": "proposal_revision", **initial_proposal, "revision": 1,
        "run_id": launch["run_id"], "parameter_space_ref_id": observed["parameter_ref_id"]}, "Proposal")
    _validate_reservations(reservations, run, trial, only_trial=original_run is not None)


def _validate_lease(lease: Json, owner_kind: str, owner_id: str, resource_id: str, deadline: str) -> None:
    require(lease, {"kind": "lease", "lease_id": lease["lease_id"], "revision": 1,
        "resource_id": resource_id, "owner_kind": owner_kind, "owner_id": owner_id,
        "epoch": 1, "deadline": deadline, "heartbeat_sequence": 0,
        "allocation_state": "active", "stop_confirmed": False}, owner_kind + " Lease")


def _validate_reservations(reservations: list[Json], run: Json, trial: Json, *,
                           only_trial: bool = False) -> None:
    domains = (("trial", trial),) if only_trial else (("task_run", run), ("trial", trial))
    for domain, owner in domains:
        value = next((value for value in reservations if value["owner_kind"] == domain), None)
        if value is None:
            raise Conflict("missing initial domain Reservation")
        expected = {"kind": "reservation", "reservation_id": owner["reservation_id"],
            "revision": 1, "owner_kind": domain, "owner_id": owner["run_id"] if domain == "task_run"
            else owner["trial_id"], "operation_ids": [], "reserved": {"credits": 0},
            "usage_revisions": [], "settlement_state": "PENDING"}
        if domain == "trial":
            expected["parent_reservation_id"] = run["reservation_id"]
        require(value, expected, "Reservation")
    if run["reservation_id"] == trial["reservation_id"]:
        raise Conflict("Run and Trial must have distinct Reservation identities")


def validate_trial_freeze(graph_value: object, preparation: Json, current: Json) -> tuple[Json, list[Json]]:
    graph = cast(Json, validate_object_graph(graph_value))
    observed = preparation["prepared"]
    request = observed["request"]
    for key in ("schema_version", "provenance", "task_contract_sources", "version_locks"):
        require(graph[key], current["graph"][key], "retained graph " + key)
    require(graph["owner_references"], observed["references"], "next Trial owner references")
    known = {object_key(value): value for value in current["graph"]["objects"]}
    proposed = {object_key(value): value for value in graph["objects"]}
    if len(proposed) != len(graph["objects"]):
        raise Conflict("duplicate immutable next-Trial graph object identity")
    for identity, value in known.items():
        require(proposed.get(identity), value, "retained historical object")
    require(proposed.get(object_key(observed["memory"])), observed["memory"], "next Task memory")
    added = [value for key, value in proposed.items() if key not in known]
    current_values = [value for value in added if value["kind"] != "task_memory_revision"]
    record = {name: _one(current_values, kind) for name, kind in (
        ("trial", "trial"), ("branch", "branch"), ("session", "agent_session"),
        ("proposal", "proposal_revision"))}
    record["run"] = current["run"]
    leases = [value for value in current_values if value["kind"] == "lease"]
    if len(leases) != 2 or record["branch"]["lease_id"] == record["session"]["workspace_lease_id"]:
        raise Conflict("next Trial requires two distinct fresh leases")
    for name, identity in (("branch_lease", record["branch"]["lease_id"]),
                           ("lease", record["session"]["workspace_lease_id"])):
        record[name] = _one([value for value in leases if value["lease_id"] == identity], "lease")
    reservations = [value for value in current_values if value["kind"] == "reservation"]
    if len(added) != 8 or len(reservations) != 1:
        raise Conflict("unexpected next-Trial objects; original Run/reservation must remain")
    launch = {**preparation["manifest"], **{key: request[key] for key in
        ("trial_id", "branch_id", "session_id", "proposal", "trial_credit_limit")}}
    observed_domain = {**current["run"], **observed, "policy_digest": current["run"]["policy_digest"],
        "version_lock": current["version_lock"], "kb_version": current["run"]["kb_version"],
        "harness_version": current["run"]["harness_version"],
        "environment_version": current["run"]["environment_version"]}
    _validate_domain(record, reservations, launch, observed_domain, preparation["source"], current["run"])
    return record, added
