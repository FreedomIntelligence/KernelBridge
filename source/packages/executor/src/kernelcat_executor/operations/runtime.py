"""Finite preparation/materialization around original owner transactions."""
from __future__ import annotations

from datetime import timedelta
from typing import cast

from kernelcat_executor.contracts.json_decoder import decode_object
from kernelcat_executor.contracts.knowledge_fences import verify_receipt
from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    Json,
    Outcome,
    Task,
    canonical,
    digest,
)
from kernelcat_executor.contracts.profiles import AgentProfile, ProfileRevision
from kernelcat_executor.contracts.runtime import (
    bytes_digest,
    normalize_program,
    require,
    validate_launch,
    validate_runtime,
)
from kernelcat_executor.contracts.runtime_ports import (
    BoundRuntimeView,
    ProgramArtifacts,
    RunJournal,
    WorkspaceMaterializer,
)
from kernelcat_executor.contracts.task_contract import load_task_contract
from kernelcat_executor.contracts.version_lock import managed_execution_identity, unknown
from kernelcat_executor.generated.python_module_observation import PythonModuleObservationReceipt
from kernelcat_executor.operations import runtime_trials
from kernelcat_executor.operations.gateway import Gateway


class RuntimeOwner:
    def __init__(self, journal: RunJournal, artifacts: ProgramArtifacts, *,
                 policy: Json, profile: AgentProfile, version_lock: Json,
                 tool_names: list[str], lease_seconds: float) -> None:
        self.journal, self.artifacts = journal, artifacts
        self.policy, self.profile, self.version_lock = policy, profile, version_lock
        self.tool_names, self.lease_seconds = tool_names, lease_seconds

    def prepare(self, value: Json) -> Json:
        request = validate_runtime(value, "PrepareRunRequest")
        launch = cast(Json, validate_launch(request["manifest"]))
        if launch["model_route"] != self.policy["routes"][0]:
            raise Conflict("manifest model_route differs from the configured initial policy route")
        old = self.journal.prepare_intent(request)
        if old is not None:
            self.verify_preparation(old)
            return self.journal.prepared(launch["intent_id"])
        source = cast(Json, load_task_contract(launch["contract_source_text"].encode("utf-8")))
        bundle = normalize_program(launch["program"])
        raw = canonical(bundle).encode("utf-8")
        baseline = source["contract"]["baseline"]
        if baseline["artifact_digest"] != bytes_digest(raw):
            raise Conflict("TaskContract baseline digest differs from real ProgramBundle bytes")
        program_ref = self.artifacts.put(raw, "program")
        task_ref = self.artifacts.put(launch["contract_source_text"].encode("utf-8"), "task-contract")
        parameter_ref = self.artifacts.put(launch["proposal"]["parameter_source"].encode("utf-8"), "parameters")
        policy_ref = self.artifacts.put(canonical(self.policy).encode("utf-8"), "policy")
        version_ref = self.artifacts.put(canonical(self.version_lock).encode("utf-8"), "version-lock")
        root: Json = {"kind": "program", "program_id": baseline["program_id"],
            "run_id": launch["run_id"], "parent_program_ids": [], "artifact_digest": bytes_digest(raw),
            "configuration_digest": bytes_digest(bundle["configuration_source"].encode("utf-8")),
            "manifest_ref_id": program_ref["ref_id"], "generation_operation_ids": [],
            "candidate_role": "root", "native_ref_ids": []}
        retained, retained_locks, root = self._retained(root, source)
        task = Task(id="runtime-task/" + digest({"run": launch["run_id"], "trial": launch["trial_id"]}),
            goal=launch["goal"], scope="runtime-scope/" + digest(launch["run_id"]),
            contract=source["contract"], seed_files={entry["path"]: entry["text"] for entry in bundle["files"]})
        revision = ProfileRevision(scope=task.scope, generation=0, profile=self.profile)
        deadline = (self.journal.clock() + timedelta(seconds=self.lease_seconds)).isoformat()
        prepared: Json = {"schema_version": "kernelcat.prepared-run/v1", "intent_id": launch["intent_id"],
            "manifest_digest": request["manifest_digest"], "host_task_id": task.id,
            "version_lock": self.version_lock, "policy_digest": "sha256:" + digest(self.policy),
            "parent_program": root, "references": [program_ref, task_ref, parameter_ref, policy_ref, version_ref],
            "parameter_ref_id": parameter_ref["ref_id"], "retained_objects": retained,
            "retained_sources": [], "retained_version_locks": retained_locks,
            "memory": {"kind": "task_memory_revision", "memory_id": "memory/" + digest(launch["run_id"]),
                "revision": 1, "run_id": launch["run_id"], "memory_epoch": 0, "fact_ids": [],
                "event_cursor": 0, "source_revisions": [], "source_ref_ids": []},
            "harness_version": self.version_lock["lock"]["executables"]["pi"]["identity"],
            "kb_version": self.version_lock["lock"]["kb_snapshot"],
            "environment_version": unknown("managed workspace is local; no device qualification observed"),
            "compiler_version": self.version_lock["lock"]["rules"]["context_compiler"],
            "branch_resource_id": "branch-control/" + digest({"run": launch["run_id"], "branch": launch["branch_id"]}),
            "branch_lease_deadline": deadline,
            "workspace_resource_id": "workspace/" + digest({"run": launch["run_id"], "trial": launch["trial_id"]}),
            "workspace_lease_deadline": deadline}
        if self.journal.snapshot()["account"].get("multi_trial"):
            prepared["required_ref_ids"] = [value["ref_id"] for value in prepared["references"]]
        record = {"prepared": prepared, "source": source, "manifest": launch,
            "task": task.model_dump(mode="json"), "policy": self.policy,
            "profile": revision.model_dump(mode="json"), "lease_seconds": self.lease_seconds,
            "tool_names": self.tool_names, "version_lock": self.version_lock,
            "required_ref_ids": [value["ref_id"] for value in prepared["references"]]}
        self.journal.finish_preparation(record)
        return self.journal.prepared(launch["intent_id"])

    def _retained(self, root: Json, source: Json) -> tuple[list[Json], list[Json], Json]:
        for run in self.journal.snapshot()["runtime_runs"].values():
            for value in run["graph"]["objects"]:
                if (value["kind"] == "program" and value["program_id"] == root["program_id"]
                        and value["candidate_role"] != "root"):
                    raise Conflict("baseline Program identity already belongs to a non-root immutable Program")
            if run["run"]["root_program_id"] != root["program_id"]:
                continue
            existing = next(value for value in run["graph"]["objects"]
                            if value["kind"] == "program" and value["program_id"] == root["program_id"])
            for key in ("artifact_digest", "configuration_digest", "manifest_ref_id", "candidate_role"):
                require(existing[key], root[key], "shared immutable root " + key)
            require(run["run"]["task_contract_hash"], source["source_digest"], "shared root TaskContract source")
            reservations = [value for value in run["graph"]["objects"] if value["kind"] == "reservation"
                            and value["reservation_id"] == run["run"]["reservation_id"]]
            retained = [run["run"], *reservations]
            lock = next(value for value in run["graph"]["version_locks"]
                        if value["version_lock_digest"] == run["run"]["version_lock_digest"])
            locks = [] if lock == self.version_lock else [lock]
            return retained, locks, existing
        return [], [], root

    def verify_configuration(self, preparation: Json) -> None:
        require(preparation["policy"], self.policy, "actual configured policy")
        require(preparation["profile"]["profile"], self.profile, "actual configured Profile")
        retained = preparation["version_lock"]
        for entry in (retained, self.version_lock):
            require(entry["version_lock_digest"], "sha256:" + digest(entry["lock"]),
                    "VersionLock evidence digest")
        require(managed_execution_identity(retained["lock"]),
                managed_execution_identity(self.version_lock["lock"]), "actual VersionLock execution inputs")

    def record_python_modules(
        self, run_id: str, trial_id: str, expected: object, report: object, *,
        expected_account_revision: int,
    ) -> PythonModuleObservationReceipt:
        """Retain startup evidence only after current configuration matches the original owner."""
        self.verify_configuration(self.journal.preparation(run_id, trial_id))
        return self.journal.record_runtime_python_modules(
            expected, report, expected_account_revision=expected_account_revision,
            run_id=run_id, trial_id=trial_id)

    def verify_preparation(self, preparation: Json) -> None:
        self.verify_configuration(preparation)
        for reference in preparation["prepared"]["references"]:
            if reference["owner"] == "program_store":
                self.artifacts.read(reference)
        source = load_task_contract(preparation["manifest"]["contract_source_text"].encode("utf-8"))
        require(source, preparation["source"], "original TaskContract source bytes")
        root = preparation["prepared"]["parent_program"]
        reference = next(value for value in preparation["prepared"]["references"]
                         if value["ref_id"] == root["manifest_ref_id"])
        bundle = self.artifacts.program(reference)
        require(root["artifact_digest"], bytes_digest(canonical(bundle).encode("utf-8")), "root bytes")

    def freeze(self, request: Json) -> Json:
        request = validate_runtime(request, "FreezeRunRequest")
        preparation = self.journal.snapshot()["runtime_prepared"].get(request["intent_id"])
        if preparation is None:
            raise Conflict("freeze has no original preparation")
        self.verify_preparation(preparation)
        return self.journal.freeze(request)

    def preparation(self, run_id: str, trial_id: str) -> Json:
        return self.journal.preparation(run_id, trial_id)

    def prepare_trial(self, value: Json) -> Json:
        request = validate_runtime(value, "PrepareTrialRequest")
        return runtime_trials.prepare_trial(self, request)

    def freeze_trial(self, value: Json) -> Json:
        request = validate_runtime(value, "FreezeTrialRequest")
        state = self.journal.snapshot()
        original = state["runtime_trial_prepared"].get(request["intent_id"])
        if original is None:
            raise Conflict("FreezeTrial has no original preparation")
        self.verify_preparation(original)
        return self.journal.freeze_trial(request)

    def knowledge_request(self, run_id: str, trial_id: str) -> Json | None:
        return self.journal.knowledge_request(run_id, trial_id)

    def prepare_knowledge(self, value: Json) -> Json:
        request = validate_runtime(value, "PrepareKnowledgeCaptureRequest")
        return self.journal.prepare_knowledge(request)

    def install_knowledge(self, value: Json) -> Json:
        request = validate_runtime(value, "InstallKnowledgeCaptureRequest")
        state = self.journal.snapshot()
        consumer = request["registration"]["consumer"]
        envelope = self.knowledge_request(consumer["run_id"], consumer["trial_id"])
        if envelope is None:
            raise Conflict("Knowledge install has no original capture envelope")
        raw = self.artifacts.read(request["capture_receipt_ref"])
        receipt = decode_object(raw)
        binding = verify_receipt(request, envelope, state["account"], receipt, raw)
        return self.journal.install_knowledge(request, binding)

    def snapshot(self, request: Json, store: BoundRuntimeView, gateway: Gateway,
                 materialize_workspace: WorkspaceMaterializer) -> Json:
        request = validate_runtime(request, "SnapshotProgramRequest")
        state = store.runtime_state()
        operation_id = "materialize/" + digest({"run": store.actor["run_id"],
            "trial": store.actor["trial_id"], "intent": request["intent_id"]})
        preparation = self.preparation(store.actor["run_id"], store.actor["trial_id"])
        action = Action(id=operation_id, task=preparation["task"]["id"], tool="__snapshot_program__",
                        arguments=request)
        previous = store.snapshot()["actions"].get(operation_id)
        if previous is not None and previous.get("terminal"):
            require(previous["action_digest"], digest(action), "historical materialization request")
            if previous["outcome"]["status"] == "completed":
                historical: Json = previous["outcome"]["data"]
                self.verify_snapshot(historical)
                return historical
        if previous is None:
            require(request["expected"], state["stamp"], "materialization execution stamp")
            if state["stamp"]["run_status"] != "generation_stopped":
                raise Conflict("generation_result must stop ordinary generation before snapshot")
        parent = preparation["prepared"]["parent_program"]
        parent_ref = next(value for value in preparation["prepared"]["references"]
                          if value["ref_id"] == parent["manifest_ref_id"])
        original = self.artifacts.program(parent_ref)

        def materialize() -> Outcome:
            bundle = materialize_workspace(original)
            raw = canonical(bundle).encode("utf-8")
            manifest_ref = self.artifacts.put(raw, "program")
            provenance = {"schema_version": "kernelcat.program-provenance/v1", "operation_id": operation_id,
                "run_id": store.actor["run_id"], "trial_id": store.actor["trial_id"],
                "program_id": request["program_id"], "parent_program_id": state["trial"]["working_program_id"],
                "task_contract_hash": state["run"]["task_contract_hash"],
                "manifest_digest": state["manifest_digest"], "artifact_digest": bytes_digest(raw)}
            provenance_ref = self.artifacts.put(canonical(provenance).encode("utf-8"), "program-provenance")
            return Outcome(data={"operation_id": operation_id, "program_id": request["program_id"],
                "artifact_digest": bytes_digest(raw),
                "configuration_digest": bytes_digest(bundle["configuration_source"].encode("utf-8")),
                "manifest_ref": manifest_ref, "provenance_ref": provenance_ref})

        result = gateway.internal(action, 0, materialize)
        if result.status == "failed":
            raise Conflict("workspace materialization failed: " + canonical(result.data))
        if result.status != "completed":
            from kernelcat_executor.contracts.models import InDoubt
            raise InDoubt("workspace materialization must reconcile its original operation")
        self.verify_snapshot(result.data)
        return result.data

    def verify_snapshot(self, result: Json) -> Json:
        bundle = self.artifacts.program(result["manifest_ref"])
        require(bytes_digest(canonical(bundle).encode("utf-8")), result["artifact_digest"], "snapshot bytes")
        require(bytes_digest(bundle["configuration_source"].encode("utf-8")),
                result["configuration_digest"], "snapshot configuration bytes")
        return decode_object(self.artifacts.read(result["provenance_ref"]))

    def adopt(self, request: Json, store: BoundRuntimeView) -> Json:
        request = validate_runtime(request, "AdoptCandidateRequest")
        operation = store.snapshot()["actions"].get(request["snapshot_operation_id"])
        if operation is None or not operation.get("terminal"):
            raise Conflict("adoption requires its original completed materialization")
        result = operation["outcome"]["data"]
        provenance = self.verify_snapshot(result)
        return self.journal.adopt(request, store.actor, result, provenance)
