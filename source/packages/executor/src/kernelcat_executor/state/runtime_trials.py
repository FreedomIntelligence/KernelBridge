"""Finite same-Run Trial transactions under the original Journal's lock."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from kernelcat_executor.contracts.knowledge_fences import (
    TRIAL_CONTROL_EVENT,
    TRIAL_FROZEN_EVENT,
    TRIAL_PREPARED_EVENT,
)
from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, digest
from kernelcat_executor.contracts.runtime import require, validate_runtime
from kernelcat_executor.state.runtime_graph import validate_trial_freeze
from kernelcat_executor.state.runtime_knowledge import check_trial_fence, trial_key
from kernelcat_executor.state.runtime_state import (
    check_expected,
    clone,
    object_key,
    pending,
    project,
)

if TYPE_CHECKING:
    from kernelcat_executor.state.runtime import RuntimeJournal


def preparation(state: Json, run_id: str, trial_id: str) -> Json:
    record = state["runtime_preparation_index"].get(trial_key(run_id, trial_id))
    if record is None:
        raise Conflict("selected Run/Trial has no completed original preparation")
    return cast(Json, record)


def active_preparation(state: Json, record: Json) -> None:
    observed = record["prepared"]
    if "request" not in observed:
        launch = record["manifest"]
        if launch["run_id"] in state["runtime_runs"]:
            raise Conflict("initial preparation is already frozen")
        return
    request = observed["request"]
    run_id = request["expected"]["run_id"]
    if state["runtime_pending_trials"].get(run_id) != request["intent_id"]:
        raise Conflict("Trial preparation was superseded or already frozen")
    if state["runtime_run_stops"].get(run_id):
        raise Conflict("global StopRun is terminal")
    check_expected(state, observed["transition_stamp"])


def _fresh_identities(state: Json, request: Json) -> None:
    fields = (("trial", "trial_id"), ("branch", "branch_id"), ("agent_session", "session_id"))
    for run in state["runtime_runs"].values():
        for value in run["graph"]["objects"]:
            for kind, field in fields:
                identity_field = "agent_session_id" if kind == "agent_session" else field
                if value["kind"] == kind and value[identity_field] == request[field]:
                    raise Conflict("next Trial requires fresh " + field)
    for admission in state["runtime_trial_admissions"].values():
        if any(admission["request"][field] == request[field] for _, field in fields):
            raise Conflict("next Trial identity already belongs to a retained preparation")
    for original in state["runtime_preparations"].values():
        if any(original["request"]["manifest"][field] == request[field] for _, field in fields):
            raise Conflict("next Trial identity already belongs to an initial preparation")


def _parent(record: Json, identity: str) -> Json:
    programs = {value["program_id"]: value for value in record["graph"]["objects"]
                if value["kind"] == "program"}
    parent = programs.get(identity)
    if parent is None:
        raise Conflict("next Trial parent is not a retained same-Run Program")
    root = record["run"]["root_program_id"]
    visited: set[str] = set()
    stack = [identity]
    while stack:
        current = stack.pop()
        if current == root:
            return cast(Json, parent)
        if current in visited:
            continue
        visited.add(current)
        value = programs.get(current)
        if value is None or value["run_id"] != record["run"]["run_id"]:
            raise Conflict("next Trial Program ancestry leaves its original Run")
        stack.extend(value["parent_program_ids"])
    raise Conflict("next Trial parent has no retained original-root ancestry")


def _admission(journal: RuntimeJournal, state: Json, request: Json, sequence: int) -> Json:
    if not state["account"].get("multi_trial"):
        raise Conflict("PrepareTrial requires an explicit fresh v3 account")
    if request["intent_id"] in state["runtime_preparations"]:
        raise Conflict("Trial preparation intent already belongs to an initial launch")
    record = check_expected(state, request["expected"])
    run_id = record["run"]["run_id"]
    require(request["manifest_digest"], record["manifest_digest"], "original Run manifest")
    if state["runtime_run_stops"].get(run_id):
        raise Conflict("global StopRun is terminal")
    if pending(state, run_id):
        raise InDoubt("PrepareTrial refuses unresolved Run effects; reconcile original work")
    previous = state["runtime_pending_trials"].get(run_id)
    if previous != request.get("supersedes_intent_id"):
        raise Conflict("pending Trial preparation requires its exact supersedes_intent_id")
    _fresh_identities(state, request)
    parent = _parent(record, request["parent_program_id"])
    deadline = (journal.clock() + timedelta(seconds=record["lease_seconds"])).isoformat()
    memory = clone(record["memory"])
    prior = [value["memory"] for value in state["runtime_trial_admissions"].values()
             if value["run_id"] == run_id]
    memory.update(revision=max([memory["revision"], *[value["revision"] for value in prior]]) + 1,
                  memory_epoch=max([memory["memory_epoch"], *[value["memory_epoch"] for value in prior]]) + 1,
                  event_cursor=sequence)
    return {"request": request, "request_digest": "sha256:" + digest(request), "run_id": run_id,
        "parent_program": parent, "memory": memory,
        "host_task_id": "runtime-task/" + digest({"run": run_id, "trial": request["trial_id"]}),
        "workspace_resource_id": "workspace/" + digest({"run": run_id, "trial": request["trial_id"]}),
        "branch_resource_id": "branch-control/" + digest({"run": run_id, "branch": request["branch_id"]}),
        "workspace_lease_deadline": deadline, "branch_lease_deadline": deadline}


def prepare_trial(journal: RuntimeJournal, value: Json) -> Json:
    request = validate_runtime(value, "PrepareTrialRequest")

    def apply(rows: list[Json]) -> tuple[Json | None, Json]:
        state = journal._state(rows)
        old = state["runtime_trial_admissions"].get(request["intent_id"])
        if old is not None:
            require(old["request"], request, "original PrepareTrial body")
            result = clone(old)
            result["existing_preparation"] = state["runtime_trial_prepared"].get(request["intent_id"])
            return None, result
        admitted = _admission(journal, state, request, len(rows))
        record = state["runtime_runs"][admitted["run_id"]]
        event = journal._control_event(state, record, request, False, journal.clock(),
                                       kind=TRIAL_CONTROL_EVENT)
        control = event["events"][-1]
        if record["trial"]["execution_state"] == "GENERATING":
            trial = {**record["trial"], "revision": record["trial"]["revision"] + 1,
                     "execution_state": "CANCELLED", "settlement_state": "SETTLED"}
            control["changes"]["trial"] = trial
            control["objects"].append(trial)
        control["preparation"] = admitted
        projected = journal._state([*rows, {**event, "seq": len(rows)}])
        admitted["transition_stamp"] = clone(cast(Json, project(projected, admitted["run_id"],
                                                              request["expected"]["trial_id"])["stamp"]))
        return event, clone(admitted)
    return journal._transaction(apply)


def finish_preparation(journal: RuntimeJournal, record: Json) -> Json:
    observed = record["prepared"]
    identity = observed["request"]["intent_id"]

    def apply(rows: list[Json]) -> tuple[Json | None, Json]:
        state = journal._state(rows)
        old = state["runtime_trial_prepared"].get(identity)
        if old is not None:
            require(old["prepared"]["request"], observed["request"], "completed Trial preparation body")
            return None, clone(old["prepared"])
        original = state["runtime_trial_admissions"].get(identity)
        if original is None:
            raise Conflict("Trial artifact preparation has no original admitted intent")
        require(original["request"], observed["request"], "admitted Trial preparation body")
        active_preparation(state, record)
        for key in ("request_digest", "transition_stamp", "memory", "parent_program", "host_task_id",
                    "workspace_resource_id", "workspace_lease_deadline", "branch_resource_id", "branch_lease_deadline"):
            require(observed[key], original[key], "admitted preparation " + key)
        observed["prepare_revision"] = len(rows)
        observed["account"] = {**journal.observation(state), "revision": len(rows)}
        event = {"kind": TRIAL_PREPARED_EVENT, "intent_id": identity, "record": record}
        return event, clone(observed)
    return journal._transaction(apply)


def prepared_trial(journal: RuntimeJournal, intent_id: str) -> Json:
    state = journal.snapshot()
    original = state["runtime_trial_prepared"].get(intent_id)
    if original is None:
        raise Conflict("Trial preparation has not completed")
    result = clone(original["prepared"])
    result["account"] = journal.observation(state)
    request = result["request"]
    run_id = request["expected"]["run_id"]
    if request["trial_id"] in state["runtime_trials"].get(run_id, {}):
        result["existing_state"] = project(state, run_id, request["trial_id"])
    return result


def _unique_added(state: Json, added: list[Json]) -> None:
    identities = {(value["kind"], object_key(value)[1]) for run in state["runtime_runs"].values()
                  for value in run["graph"]["objects"]}
    for value in added:
        if value["kind"] != "task_memory_revision" and (value["kind"], object_key(value)[1]) in identities:
            raise Conflict("next Trial graph reuses an existing immutable identity")


def freeze_trial(journal: RuntimeJournal, value: Json) -> Json:
    request = validate_runtime(value, "FreezeTrialRequest")

    def apply(rows: list[Json]) -> tuple[Json | None, Json]:
        state = journal._state(rows)
        old = journal._intent(state, TRIAL_FROZEN_EVENT, request)
        if old is not None:
            historical = journal._state(rows[:old["seq"] + 1])
            result = dict(project(historical, old["run_id"], old["record"]["trial"]["trial_id"]))
            result["freeze_replayed"] = True
            return None, result
        original = state["runtime_trial_prepared"].get(request["intent_id"])
        if original is None:
            raise Conflict("FreezeTrial has no completed original preparation")
        observed = original["prepared"]
        active_preparation(state, original)
        require(request["request_digest"], observed["request_digest"], "PrepareTrial digest")
        require(request["expected"], observed["transition_stamp"], "stopped transition stamp")
        if state["account_revision"] != request["expected_account_revision"]:
            raise Conflict("stale account revision before FreezeTrial")
        run_id = observed["request"]["expected"]["run_id"]
        if pending(state, run_id):
            raise InDoubt("FreezeTrial refuses unresolved original Run effects")
        check_trial_fence(state, run_id, observed["request"]["trial_id"])
        journal._check_prepared_knowledge(state, original)
        current = state["runtime_runs"][run_id]
        record, added = validate_trial_freeze(request["graph"], original, current)
        for name in ("lease", "branch_lease"):
            if datetime.fromisoformat(record[name]["deadline"].replace("Z", "+00:00")) <= journal.clock():
                raise Conflict("prepared next-Trial " + name + " already expired before freeze")
        _unique_added(state, added)
        record.update({key: original[key] for key in ("task", "policy", "profile", "manifest",
            "version_lock", "tool_names", "lease_seconds", "required_ref_ids")})
        record.update(memory=observed["memory"], parent_program=observed["parent_program"],
            compiler_version=current["compiler_version"], manifest_digest=current["manifest_digest"],
            generation=current["generation"] + 1, control_revision=current["control_revision"] + 1,
            status="active", trial_budget={"limit": observed["request"]["trial_credit_limit"], "spent": 0, "reserved": 0})
        known_refs = {reference["ref_id"] for reference in current["references"]}
        references = [value for value in observed["references"] if value["ref_id"] not in known_refs]
        event = {"kind": TRIAL_FROZEN_EVENT, "run_id": run_id, "request": request,
                 "record": record, "objects": added, "references": references}
        projected = journal._state([*rows, {**event, "seq": len(rows)}])
        return event, dict(project(projected, run_id, record["trial"]["trial_id"]))
    return journal._transaction(apply)
