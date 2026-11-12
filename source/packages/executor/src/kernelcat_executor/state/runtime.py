"""Managed Run transactions on the existing Journal authority, never a side ledger."""
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TypeVar, cast

from kernelcat_executor.contracts.knowledge_fences import (
    CAPTURE_INSTALLED_EVENT,
    CAPTURE_PREPARED_EVENT,
    FENCE_CONTROL_EVENT,
    FENCE_EVENT,
    KNOWLEDGE_HEADER,
    MULTI_TRIAL_HEADER,
    TRIAL_CONTROL_EVENT,
    TRIAL_FROZEN_EVENT,
    TRIAL_PREPARED_EVENT,
    fence_receipt,
    fence_request,
)
from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, Policy, digest
from kernelcat_executor.contracts.python_module_observation import EVENT_KIND, observation_evidence
from kernelcat_executor.contracts.runtime import require, validate_runtime
from kernelcat_executor.generated.knowledge_runtime import KnowledgeFenceReceipt
from kernelcat_executor.generated.python_module_observation import PythonModuleObservationReceipt
from kernelcat_executor.state import python_module_observations, runtime_capture, runtime_trials
from kernelcat_executor.state.profile_journal import AgentJournal
from kernelcat_executor.state.runtime_graph import validate_freeze
from kernelcat_executor.state.runtime_knowledge import (
    check_registration,
    check_trial_fence,
    trial_key,
)
from kernelcat_executor.state.runtime_state import (
    actor_from,
    apply_runtime,
    budget,
    check_actor,
    check_expected,
    clone,
    object_key,
    pending,
    project,
    retain,
)

T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic(events: list[Json]) -> Json:
    return {"kind": "atomic", "events": events}


class RuntimeJournal(AgentJournal):
    initial_kinds = frozenset({"runtime_account_initialized/v1", KNOWLEDGE_HEADER, MULTI_TRIAL_HEADER})
    v2_event_kinds = frozenset({"requested", "started", "admitted", "settled", "uncertain",
        "cancelled_unstarted", "rejected", "installed", "task", "runtime",
        "agent_profile_initialized", "agent_profile_pinned", "agent_profile_activated",
        "runtime_prepare_intent", "runtime_prepared", "runtime_frozen", "runtime_context",
        "runtime_control", "runtime_candidate", "runtime_finalized", "runtime_generation_stopped",
        FENCE_EVENT, FENCE_CONTROL_EVENT, EVENT_KIND})
    v3_event_kinds = v2_event_kinds | frozenset({TRIAL_CONTROL_EVENT, TRIAL_PREPARED_EVENT,
        TRIAL_FROZEN_EVENT, CAPTURE_PREPARED_EVENT, CAPTURE_INSTALLED_EVENT})
    application_event_kinds = frozenset("application/" + kind for kind in (
        "created", "input_accepted", "input_transition", "lease_granted", "lease_revoked",
        "goal", "task_version", "trial", "call_intent", "call_result", "call_reconciled", "checkpoint", "configuration", "evidence"))
    v2_event_kinds |= application_event_kinds
    v3_event_kinds |= application_event_kinds

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(path.resolve())
        self.clock = clock

    def _transaction(self, fn: Callable[[list[Json]], tuple[Json | None, T]], *, shared: bool = False) -> T:
        def fenced(rows: list[Json]) -> tuple[Json | None, T]:
            if rows:
                header = rows[0]
                if header["kind"] not in self.initial_kinds:
                    raise Conflict("managed runtime refuses a legacy ledger; no migration is implied")
                identity = self._authority(header["account"]["intent_id"])
                if identity != header["account"]["authority_digest"]:
                    raise Conflict("runtime account canonical authority path changed; copied ledger refused")
                self._check_format(rows)
            return fn(rows)
        return super()._transaction(fenced, shared=shared)

    @classmethod
    def _check_format(cls, rows: list[Json]) -> None:
        header = rows[0]
        account = validate_runtime(header["account"], "AccountReceipt")
        if account.get("multi_trial"):
            if header["kind"] != MULTI_TRIAL_HEADER:
                raise Conflict("runtime account multi-Trial header format mismatch")
            for event in cls._events(rows[1:]):
                if event["kind"] not in cls.v3_event_kinds:
                    raise Conflict("unsupported required v3 Journal event: " + event["kind"])
            return
        managed_knowledge = "knowledge_authority_id" in account
        if managed_knowledge != (header["kind"] == KNOWLEDGE_HEADER) or header["kind"] == MULTI_TRIAL_HEADER:
            raise Conflict("runtime account header/Knowledge authority format mismatch")
        if header["kind"] == KNOWLEDGE_HEADER:
            for event in cls._events(rows[1:]):
                if event["kind"] not in cls.v2_event_kinds:
                    raise Conflict("unsupported required v2 Journal event: " + event["kind"])

    @classmethod
    def _state(cls, rows: list[Json]) -> Json:
        state = apply_runtime(super()._state(rows), rows)
        python_module_observations.validate_owner_links(state, cls._events(rows), managed=True)
        return state

    def record_runtime_python_modules(
        self, expected: object, report: object, *, expected_account_revision: int,
        run_id: str | None = None, trial_id: str | None = None,
    ) -> PythonModuleObservationReceipt:
        """Retain one account/preparation observation under the original revision CAS."""
        expected_value, report_value = observation_evidence(expected, report)
        if type(expected_account_revision) is not int or expected_account_revision < 0:
            raise ValueError("module observation requires a nonnegative account revision")
        if (run_id is None) != (trial_id is None):
            raise ValueError("module observation requires both Run and Trial identities")

        def apply(rows: list[Json]) -> tuple[Json | None, PythonModuleObservationReceipt]:
            state = self._state(rows)
            if state["account_revision"] != expected_account_revision:
                raise Conflict("module observation account revision changed")
            preparation = None
            if run_id is not None and trial_id is not None:
                preparation = runtime_trials.preparation(state, run_id, trial_id)
            binding = python_module_observations.managed_binding(state["account"], preparation)
            return python_module_observations.observation_event(
                state, len(rows), binding, expected_value, report_value,
                owner_events=self._events(rows))
        return self._transaction(apply)

    def _authority(self, intent_id: str) -> str:
        return "sha256:" + digest({"canonical_journal_path": str(self.path), "intent_id": intent_id})

    def initialize(self, limit: int, policy: Policy, *,
                   resource_limits: dict[str, int] | None = None) -> None:
        raise Conflict("managed runtime requires explicit account_create; implicit funding refused")

    def create_account(self, request: Json) -> Json:
        request = validate_runtime(request, "AccountCreateRequest")
        authority = self._authority(request["intent_id"])
        account = {"schema_version": "kernelcat.runtime-account/v1",
            "account_id": "account/" + authority.removeprefix("sha256:"),
            "authority_digest": authority, "intent_id": request["intent_id"],
            "credit_limit": request["credits"]}
        kind = "runtime_account_initialized/v1"
        if "knowledge_authority_id" in request:
            kind = KNOWLEDGE_HEADER
            account["knowledge_authority_id"] = request["knowledge_authority_id"]
        if request.get("multi_trial"):
            kind = MULTI_TRIAL_HEADER
            account["multi_trial"] = True
        initial = {"kind": kind, "account": account,
            "request": request, "limit": request["credits"], "policy": Policy().model_dump(mode="json")}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass

        def apply(rows: list[Json]) -> tuple[Json | None, Json]:
            if rows:
                require(rows[0]["request"], request, "account create intent")
            return (None if rows else initial), account
        return self._transaction(apply)

    def knowledge_fence(self, value: Json) -> KnowledgeFenceReceipt:
        request = cast(Json, fence_request(value))

        def apply(rows: list[Json]) -> tuple[Json | None, KnowledgeFenceReceipt]:
            state = self._state(rows)
            previous = state["runtime_knowledge_fences"].get(request["fence_id"])
            if previous is not None:
                require(previous["request"], request, "Knowledge fence body")
                return None, cast(KnowledgeFenceReceipt, clone(previous["receipt"]))
            self._check_fence_account(rows[0], request)
            check_registration(state, request)
            event = self._fence_event(state, request)
            row = {**event, "seq": len(rows), "prev": rows[-1]["digest"]}
            row["digest"] = digest(row)
            return event, fence_receipt(request, row)
        return self._transaction(apply)

    @staticmethod
    def _check_fence_account(header: Json, request: Json) -> None:
        registration = request["registration"]
        consumer = registration["consumer"]
        account = header["account"]
        if header["kind"] not in {KNOWLEDGE_HEADER, MULTI_TRIAL_HEADER} or "knowledge_authority_id" not in account:
            raise Conflict("Knowledge fence requires an explicit fresh v2/v3 pinned account")
        require(consumer["required_journal_header"], header["kind"], "required Journal header")
        require(consumer["journal_authority_digest"], account["authority_digest"], "Journal authority")
        require(consumer["account_id"], account["account_id"], "Knowledge consumer account")
        require(registration["authority_id"], account["knowledge_authority_id"], "Knowledge authority")

    def _fence_event(self, state: Json, request: Json) -> Json:
        consumer = request["registration"]["consumer"]
        event = {"kind": FENCE_EVENT, "request": request}
        key = trial_key(consumer["run_id"], consumer["trial_id"])
        record = state["runtime_runs"].get(consumer["run_id"])
        if (key in state["runtime_knowledge_trials"] or record is None
                or record["trial"]["trial_id"] != consumer["trial_id"]):
            return event
        stop = {"intent_id": "knowledge-fence/" + request["fence_id"], "run_id": consumer["run_id"],
            "expected_control_revision": record["control_revision"],
            "reason": "Knowledge dependency permanently invalidated"}
        # Event kind scopes replay identity. Public callers may use any valid
        # intent string without reserving or replacing Knowledge-owned control.
        control = self._control_event(state, record, stop, False, self.clock(),
                                      kind=FENCE_CONTROL_EVENT)
        return atomic([*control["events"], event])

    @staticmethod
    def observation(state: Json) -> Json:
        return {"account": state["account"], "revision": state["account_revision"], "budget": budget(state)}

    def prepare_intent(self, request: Json) -> Json | None:
        launch = request["manifest"]

        def apply(rows: list[Json]) -> tuple[Json | None, Json | None]:
            state = self._state(rows)
            if launch["account_id"] != state["account"]["account_id"]:
                raise Conflict("manifest does not bind this runtime account")
            old = state["runtime_preparations"].get(launch["intent_id"])
            if old is not None:
                require(old["request"], request, "preparation intent body")
                return None, state["runtime_prepared"].get(launch["intent_id"])
            if state["account"].get("multi_trial"):
                if launch["intent_id"] in state["runtime_trial_admissions"]:
                    raise Conflict("v3 initial intent already belongs to a Trial preparation")
                for previous in state["runtime_preparations"].values():
                    original = previous["request"]["manifest"]
                    if any(original[key] == launch[key] for key in
                           ("run_id", "trial_id", "branch_id", "session_id")):
                        raise Conflict("v3 initial identity already belongs to another original preparation")
                for previous in state["runtime_trial_admissions"].values():
                    if any(previous["request"][key] == launch[key] for key in
                           ("trial_id", "branch_id", "session_id")):
                        raise Conflict("v3 initial identity already belongs to a retained Trial preparation")
            return {"kind": "runtime_prepare_intent", "intent_id": launch["intent_id"],
                    "request": request}, None
        return self._transaction(apply)

    def finish_preparation(self, record: Json) -> Json:
        identity = record["manifest"]["intent_id"]

        def apply(rows: list[Json]) -> tuple[Json | None, Json]:
            state = self._state(rows)
            original = state["runtime_preparations"].get(identity)
            if original is None:
                raise Conflict("artifact preparation has no admitted intent")
            old = state["runtime_prepared"].get(identity)
            if old is not None:
                return None, old["prepared"]
            prepared = record["prepared"]
            prepared["prepare_revision"] = len(rows)
            prepared["memory"]["event_cursor"] = len(rows)
            prepared["account"] = {**self.observation(state), "revision": len(rows)}
            event = {"kind": "runtime_prepared", "intent_id": identity, **record}
            # A later preparation must preserve replay of already retained observations.
            self._state([*rows, {**event, "seq": len(rows)}])
            return event, prepared
        return self._transaction(apply)

    def prepared(self, intent_id: str) -> Json:
        state = self.snapshot()
        original = state["runtime_prepared"].get(intent_id)
        if original is None:
            raise Conflict("Run preparation has not completed")
        result = clone(original["prepared"])
        result["account"] = self.observation(state)
        launch = original["manifest"]
        if launch["run_id"] in state["runtime_runs"]:
            current_trial = state["runtime_runs"][launch["run_id"]]["trial"]["trial_id"]
            result["existing_state"] = project(state, launch["run_id"], current_trial)
        return result

    def freeze(self, request: Json) -> Json:
        def apply(rows: list[Json]) -> tuple[Json | None, Json]:
            state = self._state(rows)
            old = self._intent(state, "runtime_frozen", request)
            if old is not None:
                original = self._state(rows[:old["seq"] + 1])
                launch = old["record"]["manifest"]
                historical = clone(cast(Json, project(original, launch["run_id"], launch["trial_id"])))
                if state["account"].get("multi_trial"):
                    historical["freeze_replayed"] = True
                return None, historical
            preparation = state["runtime_prepared"].get(request["intent_id"])
            if preparation is None or preparation["prepared"]["manifest_digest"] != request["manifest_digest"]:
                raise Conflict("freeze does not identify its original preparation")
            if state["account_revision"] != request["expected_account_revision"]:
                raise Conflict("stale account revision before freeze")
            self._check_prepared_knowledge(state, preparation)
            record = validate_freeze(request["graph"], preparation)
            if state["account"].get("multi_trial"):
                record["memory"] = preparation["prepared"]["memory"]
            check_trial_fence(state, record["run"]["run_id"], record["trial"]["trial_id"])
            for name in ("lease", "branch_lease"):
                if datetime.fromisoformat(record[name]["deadline"].replace("Z", "+00:00")) <= self.clock():
                    raise Conflict(f"prepared {name} already expired before freeze")
            self._unique_graph(state, record)
            record.update({key: preparation[key] for key in ("task", "policy", "profile", "manifest",
                    "version_lock", "tool_names", "lease_seconds", "required_ref_ids")})
            event = {"kind": "runtime_frozen", "request": request, "record": record}
            projected = self._state([*rows, {**event, "seq": len(rows)}])
            return event, clone(cast(Json, project(projected, record["run"]["run_id"], record["trial"]["trial_id"])))
        return self._transaction(apply)

    @staticmethod
    def _unique_graph(state: Json, record: Json) -> None:
        for old in state["runtime_runs"].values():
            for name, field in (("run", "run_id"), ("trial", "trial_id"), ("branch", "branch_id"),
                                ("session", "agent_session_id")):
                if old[name][field] == record[name][field]:
                    raise Conflict(f"runtime {name} identity already belongs to another freeze")
            old_leases = {value["lease_id"] for value in old["graph"]["objects"] if value["kind"] == "lease"}
            if any(record[name]["lease_id"] in old_leases for name in ("lease", "branch_lease")):
                raise Conflict("runtime lease identity already belongs to another freeze")
        known = {object_key(value): value for run in state["runtime_runs"].values()
                 for value in run["graph"]["objects"]}
        for value in record["graph"]["objects"]:
            previous = known.get(object_key(value))
            if previous is not None:
                require(value, previous, "global immutable object identity")
        if record["manifest_digest"] in {old["manifest_digest"] for old in state["runtime_runs"].values()}:
            raise Conflict("launch manifest already frozen under another intent")

    @staticmethod
    def _intent(state: Json, kind: str, request: Json) -> Json | None:
        old = state["runtime_intents"].get(kind + "/" + request["intent_id"])
        if old is not None:
            require(old["request"], request, "control/materialization intent body")
        return cast(Json | None, old)

    def preparation(self, run_id: str, trial_id: str) -> Json:
        return clone(runtime_trials.preparation(self.snapshot(), run_id, trial_id))

    def prepare_trial(self, request: Json) -> Json:
        return runtime_trials.prepare_trial(self, request)

    def finish_trial_preparation(self, record: Json) -> Json:
        return runtime_trials.finish_preparation(self, record)

    def prepared_trial(self, intent_id: str) -> Json:
        return runtime_trials.prepared_trial(self, intent_id)

    def freeze_trial(self, request: Json) -> Json:
        return runtime_trials.freeze_trial(self, request)

    def knowledge_request(self, run_id: str, trial_id: str) -> Json | None:
        request = self.snapshot()["runtime_capture_requests"].get(trial_key(run_id, trial_id))
        return None if request is None else clone(request)

    def prepare_knowledge(self, request: Json) -> Json:
        return runtime_capture.prepare_knowledge(self, request)

    def install_knowledge(self, request: Json, receipt: Json) -> Json:
        return runtime_capture.install_knowledge(self, request, receipt)

    @staticmethod
    def _check_prepared_knowledge(state: Json, preparation: Json) -> None:
        runtime_capture.check_prepared_knowledge(state, preparation)

    def state(self, run_id: str, trial_id: str) -> Json:
        return dict(project(self.snapshot(), run_id, trial_id))

    def bind_finalization(self, expected: Json) -> Json:
        def apply(rows: list[Json]) -> tuple[Json | None, Json]:
            state = self._state(rows)
            record = check_expected(state, expected)
            if record["status"] != "generation_stopped":
                raise Conflict("finite finalization requires generation_stopped")
            check_actor(state, actor_from(expected), self.clock(), materialization=True)
            if pending(state, expected["run_id"]):
                raise InDoubt("finite finalization has unresolved original effects")
            return None, clone(cast(Json, project(state, expected["run_id"], expected["trial_id"])))
        return self._transaction(apply)

    def control(self, request: Json, *, replace: bool) -> Json:
        kind = "runtime_control"

        def apply(rows: list[Json]) -> tuple[Json | None, Json]:
            state = self._state(rows)
            old = self._intent(state, kind, request)
            if old is not None:
                run = state["runtime_runs"][old["run_id"]]
                if run.get("last_control_request") != request:
                    raise Conflict("control intent was superseded; old replay cannot regain authority")
                if replace and run["session"]["agent_session_id"] != request["session_id"]:
                    raise Conflict("replacement Session is no longer current")
                return None, clone(cast(Json, project(state, run["run"]["run_id"], run["trial"]["trial_id"])))
            if replace:
                record = check_expected(state, request["expected"])
                check_trial_fence(state, request["expected"]["run_id"], request["expected"]["trial_id"])
                if record.get("stopped") or record["lease"]["allocation_state"] == "released":
                    raise Conflict("stopped/completed Run cannot replace its Session")
                self._new_session_id(state, request["session_id"])
            else:
                record = state["runtime_runs"].get(request["run_id"])
                if record is None or request["expected_control_revision"] != record["control_revision"]:
                    raise Conflict("stale Run control revision")
            event = self._control_event(state, record, request, replace, self.clock())
            projected = self._state([*rows, {**event, "seq": len(rows)}])
            return event, clone(cast(Json, project(projected, record["run"]["run_id"], record["trial"]["trial_id"])))
        return self._transaction(apply)

    @staticmethod
    def _new_session_id(state: Json, identity: str) -> None:
        if any(value["kind"] == "agent_session" and value["agent_session_id"] == identity
               for run in state["runtime_runs"].values() for value in run["graph"]["objects"]):
            raise Conflict("replacement Session identity already exists")

    @staticmethod
    def _control_event(state: Json, record: Json, request: Json,
                       replace: bool, now: datetime, *,
                       kind: Literal["runtime_control", "runtime_knowledge_control/v1", "runtime_trial_control/v1"] = "runtime_control") -> Json:
        run_id = record["run"]["run_id"]
        unstarted = [attempt for attempt in pending(state, run_id) if not attempt.get("started")
                     and not attempt.get("outcome")]
        cancellations = [{"kind": "cancelled_unstarted", "action_id": item["action"]["id"],
            "outcome": {"status": "failed", "data": {"cancelled_unstarted": True,
                "control_intent_id": request["intent_id"]}}} for item in unstarted]
        performed = [item for item in pending(state, run_id) if item.get("started") or item.get("outcome")]
        session = {**record["session"], "revision": record["session"]["revision"] + 1,
                   "state": "INTERRUPTING" if performed else "STOPPED"}
        leases = RuntimeJournal._controlled_leases(record, request, replace, bool(performed), now)
        branch = {**record["branch"], "revision": record["branch"]["revision"] + 1}
        objects = [session]
        changes: Json = {"control_revision": record["control_revision"] + 1,
            "generation": record["generation"] + 1, "last_control_intent_id": request["intent_id"],
            "last_control_request": request}
        if replace:
            session = {**session, "agent_session_id": request["session_id"], "revision": 1,
                       "state": "RUNNING"}
            changes["status"] = record["status"]
        else:
            changes.update(status="stopped", stopped=True)
            branch["state"] = "CLOSING" if performed else "PARKED"
        changes.update(session=session, branch=branch, **leases)
        objects.extend([session, branch, *leases.values()])
        control = {"kind": kind, "run_id": run_id, "request": request,
                   "changes": changes, "objects": objects}
        return atomic([*cancellations, control])

    @staticmethod
    def _controlled_leases(record: Json, request: Json, replace: bool,
                           performed: bool, now: datetime) -> Json:
        leases = {name: {**record[name], "revision": record[name]["revision"] + 1,
                        "epoch": record[name]["epoch"] + 1} for name in ("lease", "branch_lease")}
        if replace:
            leases["lease"]["owner_id"] = request["session_id"]
            # Renewal permits only this new assignment. Original started effects still quarantine both.
            deadline = (now + timedelta(seconds=record["lease_seconds"])).isoformat()
            for lease in leases.values():
                lease["deadline"] = deadline
        else:
            for lease in leases.values():
                lease.update(allocation_state="reclaiming" if performed else "released",
                             stop_confirmed=not performed)
        return leases

    def generation_stopped(self, actor: Json) -> Json:
        def apply(rows: list[Json]) -> tuple[Json | None, Json]:
            state = self._state(rows)
            record = check_actor(state, actor, self.clock(), materialization=True)
            event = None if record["status"] == "generation_stopped" else {
                "kind": "runtime_generation_stopped", "run_id": actor["run_id"]}
            result = clone(cast(Json, project(state, actor["run_id"], actor["trial_id"])))
            if event is not None:
                result["stamp"]["run_status"] = "generation_stopped"
            return event, result
        return self._transaction(apply)

    def adopt(self, request: Json, actor: Json, verified: Json, provenance: Json) -> Json:
        def apply(rows: list[Json]) -> tuple[Json | None, Json]:
            state = self._state(rows)
            if any(request["expected"][key] != actor[key] for key in ("run_id", "trial_id")):
                raise Conflict("candidate expectation is outside the bound Run/Trial")
            old = self._intent(state, "runtime_candidate", request)
            if old is not None:
                return None, clone(cast(Json, project(state, actor["run_id"], actor["trial_id"])))
            record = check_actor(state, actor, self.clock(), materialization=True)
            check_expected(state, request["expected"])
            operation = state["actions"].get(request["snapshot_operation_id"])
            if operation is None or not operation.get("terminal") or operation["outcome"]["status"] != "completed":
                raise Conflict("candidate snapshot is not a completed original operation")
            require(operation["outcome"]["data"], verified, "snapshot receipt")
            require(operation["binding"]["run_id"], actor["run_id"], "snapshot Run")
            require(operation["binding"]["trial_id"], actor["trial_id"], "snapshot Trial")
            parent = self._snapshot_parent(record, operation, verified, provenance)
            program = request["program"]
            expected = {"kind": "program", "program_id": verified["program_id"],
                "run_id": actor["run_id"], "trial_id": actor["trial_id"],
                "parent_program_ids": [parent],
                "artifact_digest": verified["artifact_digest"],
                "configuration_digest": verified["configuration_digest"],
                "manifest_ref_id": verified["manifest_ref"]["ref_id"],
                "generation_operation_ids": [request["snapshot_operation_id"]],
                "candidate_role": "candidate", "native_ref_ids": [verified["provenance_ref"]["ref_id"]]}
            require(program, expected, "candidate Program provenance")
            self._new_program(state, program)
            trial = {**record["trial"], "revision": record["trial"]["revision"] + 1,
                "working_program_id": program["program_id"], "execution_state": "CANDIDATES_READY"}
            session = {**record["session"], "revision": record["session"]["revision"] + 1,
                       "active_trial": {"trial_id": actor["trial_id"], "revision": trial["revision"]}}
            session.pop("active_context", None)
            reservation = self._generation_reservation(record, request["snapshot_operation_id"])
            event = {"kind": "runtime_candidate", "run_id": actor["run_id"], "request": request,
                "changes": {"trial": trial, "session": session, "last_candidate_adoption": request},
                "objects": [program, trial, session, reservation],
                "references": [verified["manifest_ref"], verified["provenance_ref"]]}
            retain(clone(record["graph"]), event["objects"])
            projected = self._state([*rows, {**event, "seq": len(rows)}])
            return event, clone(cast(Json, project(projected, actor["run_id"], actor["trial_id"])))
        return self._transaction(apply)

    @staticmethod
    def _snapshot_parent(record: Json, operation: Json, verified: Json, provenance: Json) -> str:
        captured = operation["action"]["arguments"]["expected"]
        trials = [value for value in record["graph"]["objects"] if value["kind"] == "trial"
                  and value["trial_id"] == captured["trial_id"] and value["revision"] == captured["trial_revision"]]
        if len(trials) != 1:
            raise Conflict("snapshot original Trial revision is not retained")
        parent = str(trials[0]["working_program_id"])
        require(provenance, {"schema_version": "kernelcat.program-provenance/v1",
            "operation_id": operation["action"]["id"], "run_id": record["run"]["run_id"],
            "trial_id": record["trial"]["trial_id"], "program_id": verified["program_id"],
            "parent_program_id": parent, "task_contract_hash": record["run"]["task_contract_hash"],
            "manifest_digest": record["manifest_digest"], "artifact_digest": verified["artifact_digest"]},
            "original materialization provenance")
        require(parent, record["trial"]["working_program_id"], "snapshot parent no longer current")
        return parent

    @staticmethod
    def _generation_reservation(record: Json, operation_id: str) -> Json:
        snapshots = [value for value in record["graph"]["objects"] if value["kind"] == "reservation"
                     and value["reservation_id"] == record["trial"]["reservation_id"]]
        previous = max(snapshots, key=lambda value: value["revision"])
        operations = previous["operation_ids"]
        if operation_id in operations or len(operations) >= 1024:
            raise Conflict("candidate generation reference already adopted or at capacity")
        # This is a finite reference snapshot, not a second credit or usage ledger.
        return {**previous, "revision": previous["revision"] + 1,
                "operation_ids": [*operations, operation_id], "settlement_state": "SETTLED"}

    def finalize(self, actor: Json) -> Json:
        def apply(rows: list[Json]) -> tuple[Json | None, Json]:
            state = self._state(rows)
            current = clone(cast(Json, project(state, actor["run_id"], actor["trial_id"])))
            record = state["runtime_runs"][actor["run_id"]]
            stamp: Json = cast(Json, current["stamp"])
            if (current["pending_effect_ids"] or stamp["generation"] != actor["generation"]
                    or stamp["lease_epoch"] != actor["lease_epoch"]
                    or stamp["session"]["agent_session_id"] != actor["session_id"]
                    or record["status"] != "generation_stopped"
                    or record["lease"]["allocation_state"] == "released"):
                return None, current
            leases = {name: {**record[name], "revision": record[name]["revision"] + 1,
                            "allocation_state": "released", "stop_confirmed": True}
                      for name in ("lease", "branch_lease")}
            session = {**record["session"], "revision": record["session"]["revision"] + 1,
                       "state": "STOPPED"}
            branch = {**record["branch"], "revision": record["branch"]["revision"] + 1,
                      "state": "PARKED"}
            event = {"kind": "runtime_finalized", "run_id": actor["run_id"],
                "changes": {**leases, "session": session, "branch": branch},
                "objects": [*leases.values(), session, branch]}
            projected = self._state([*rows, {**event, "seq": len(rows)}])
            return event, clone(cast(Json, project(projected, actor["run_id"], actor["trial_id"])))
        return self._transaction(apply)

    @staticmethod
    def _new_program(state: Json, program: Json) -> None:
        if any(value["kind"] == "program" and value["program_id"] == program["program_id"]
               for run in state["runtime_runs"].values() for value in run["graph"]["objects"]):
            raise Conflict("immutable Program identity already exists")
