"""Finite runtime bootstrap and handoff on one already-framed Worker connection."""
from __future__ import annotations

from contextlib import ExitStack
from functools import partial
from pathlib import Path

from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts, workspace_bundle
from kernelcat_executor.capabilities.skills import Skills
from kernelcat_executor.capabilities.workspace_tools import builtin_tools
from kernelcat_executor.capabilities.workspace_policy import WorkspaceGuard
from kernelcat_executor.contracts.models import Conflict, Json, Task, canonical, digest
from kernelcat_executor.contracts.ports import Store, Tool
from kernelcat_executor.contracts.runtime import PROGRAM_BYTES, validate_runtime
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.runtime import RuntimeOwner
from kernelcat_executor.operations.session import ExecutionSession, RPCHandler
from kernelcat_executor.profiles.evidence import ReviewExperience
from kernelcat_executor.profiles.tools import ProposeImprovement, ReadLearnedSkill
from kernelcat_executor.state.runtime import RuntimeJournal
from kernelcat_executor.state.runtime_state import actor_from, check_actor
from kernelcat_executor.state.runtime_store import RuntimeStore

from kernelcat_worker.bindings import _build_proposers, _preflight, _version_inputs
from kernelcat_worker.business_tools import configured_tools
from kernelcat_worker.candidate_retention import RequiredChecksGuard, retained_business_bundle
from kernelcat_worker.configuration import Config
from kernelcat_worker.knowledge import KnowledgeReadSurface, knowledge_tools
from kernelcat_worker.managed_input import enqueue_managed_input
from kernelcat_worker.managed_knowledge import ManagedKnowledge
from kernelcat_worker.replay import NativeReplayFiles
from kernelcat_worker.rsi_effects import RsiEffects
from kernelcat_worker.rsi_experiments import ExperimentTool, SourceGuard
from kernelcat_worker.rsi_records import Records
from kernelcat_worker.rsi_retained import retained_bundle
from kernelcat_worker.startup import PythonStartup, admit_config
from kernelcat_worker.versions import collect_version_lock


class RuntimeService:
    def __init__(self, config: Config, base: Path, *, manifest: Json | None = None,
                 manifest_digest: str | None = None, startup: PythonStartup | None = None) -> None:
        self.startup = admit_config(config) if startup is None else startup
        config = self.startup.bind_config(config)
        if config.evaluator is not None or (config.self_evolution is not None
                                           and config.self_evolution.validator is not None):
            raise ValueError("managed runtime refuses generic unqualified evaluator/validator configuration")
        self.config, self.base = config, base
        self.manifest, self.manifest_digest = manifest, manifest_digest
        self.journal = RuntimeJournal((base / config.state_dir) / "events.jsonl")
        self.artifacts = RuntimeArtifacts((base / config.state_dir) / "runtime-artifacts")
        self.knowledge = ManagedKnowledge(config, base, self.journal, self.artifacts,
                                          startup=self.startup)
        self.selected_trial_id = None if manifest is None else manifest["trial_id"]
        self.owner: RuntimeOwner | None = None
        self.store: RuntimeStore | None = None
        self.workspace: Workspace | None = None
        self.gateway: Gateway | None = None
        self.host: ExecutionSession | None = None
        self.handler: RPCHandler | None = None
        self.stack = ExitStack()
        self.generation: Json | None = None
        if manifest is not None:
            self._prepare_owner()

    def _prepare_owner(self) -> None:
        config = self.config
        assert self.manifest is not None
        self.knowledge.verify_root(self.manifest["run_id"], self.manifest["trial_id"])
        _preflight(config, self.base)
        skills = Skills(tuple(self.base / root for root in config.skill_roots))
        workspace = Workspace(self.base / config.workspace / "unbound", PROGRAM_BYTES, create=False)
        # Inspection only; no writable workspace exists before freeze/seed intents.
        tools = self._tools(self.journal, workspace, skills, self.knowledge.application)
        catalogue = Gateway(self.journal, tools, "runtime-catalogue").specifications()
        names = [spec.name for spec in catalogue]
        catalogue_digest = "sha256:" + digest([spec.model_dump(mode="json") for spec in catalogue])
        inputs = _version_inputs(config, self.base, skills)
        prepared = self.journal.snapshot()["runtime_prepared"].get(self.manifest["intent_id"])
        # Sealed definitions are independent of workspace/Store identities.
        capture = collect_version_lock(config, inputs, (self.base / config.pi_worker.root).resolve(),
            tool_schema_digest=catalogue_digest, probe_executables=False, probe_source=prepared is None)
        lock = {"lock": capture.lock, "version_lock_digest": "sha256:" + digest(capture.lock)}
        self.owner = RuntimeOwner(self.journal, self.artifacts,
            policy=config.policy.model_dump(mode="json"), profile=config.agent_profile,
            version_lock=lock, tool_names=names, lease_seconds=config.pi_worker.timeout)

    def _tools(self, store: Store, workspace: Workspace, skills: Skills,
               knowledge: KnowledgeReadSurface | None) -> list[Tool]:
        tools: list[Tool] = [*builtin_tools(workspace, skills), ReadLearnedSkill(store)]
        if self.config.subagents.enabled:
            tools.append(SubagentEffect(self.config.subagents.model_dump()))
        tools.extend(configured_tools(self.config.business_tools, workspace,
            (self.base / self.config.state_dir).resolve()))
        if knowledge is not None:
            tools.extend(knowledge_tools(knowledge))
        if self.config.rsi is not None:
            tools.append(ExperimentTool(store, workspace, self.artifacts, self.config.rsi))
        if self.config.self_evolution is not None:
            evidence = self.config.self_evolution.evidence_tools
            tools.extend([ReviewExperience(store, evidence), ProposeImprovement(store, evidence)])
        return tools

    def _managed_owner(self) -> RuntimeOwner:
        if self.owner is None or self.manifest is None:
            raise Conflict("managed Run method requires a verified active launch manifest")
        return self.owner

    def _record_modules(self, run_id: str | None = None, trial_id: str | None = None) -> None:
        revision = self.journal.snapshot()["account_revision"]
        if run_id is None:
            self.journal.record_runtime_python_modules(
                self.startup.expected, self.startup.report, expected_account_revision=revision)
        else:
            assert trial_id is not None
            self._managed_owner().record_python_modules(
                run_id, trial_id, self.startup.expected, self.startup.report,
                expected_account_revision=revision)

    def _select_trial(self, trial_id: str) -> None:
        if self.store is not None and self.store.actor["trial_id"] != trial_id:
            raise Conflict("one stream cannot switch its already-bound execution Actor")
        self.selected_trial_id = trial_id

    def _manifest_preparation(self, run_id: str, trial_id: str) -> Json:
        self._managed_owner()
        assert self.manifest is not None
        if run_id != self.manifest["run_id"]:
            raise Conflict("binding request is outside the active connection manifest")
        preparation = self._managed_owner().preparation(run_id, trial_id)
        prepared = preparation["prepared"]
        checksum = (prepared["manifest_digest"] if prepared["schema_version"] == "kernelcat.prepared-run/v1"
                    else prepared["request"]["manifest_digest"])
        if preparation["manifest"] != self.manifest or checksum != self.manifest_digest:
            raise Conflict("binding has no verified original manifest preparation")
        return dict(preparation)

    def _verified_preparation(self, run_id: str, trial_id: str) -> Json:
        preparation = self._manifest_preparation(run_id, trial_id)
        self._managed_owner().verify_preparation(preparation)
        return preparation

    def _workspace(self, actor: Json) -> Workspace:
        return Workspace((self.base / self.config.workspace) /
            digest({"run": actor["run_id"], "trial": actor["trial_id"]}), PROGRAM_BYTES, create=False)

    def _binding_inputs(self, state: Json, *, replacement: bool) -> tuple[Json, Json]:
        assert self.manifest is not None
        preparation = self._verified_preparation(state["run"]["run_id"], state["trial"]["trial_id"])
        current = self.journal.state(state["run"]["run_id"], state["trial"]["trial_id"])
        if current["manifest_digest"] != self.manifest_digest:
            raise Conflict("connection manifest does not own the frozen Run")
        stamp = state["stamp"]
        actor = actor_from(stamp)
        if actor_from(current["stamp"]) != actor:
            raise Conflict("runtime assignment was superseded before binding")
        prepared = preparation["prepared"]
        session_id = (self.manifest["session_id"] if prepared["schema_version"] == "kernelcat.prepared-run/v1"
                      else prepared["request"]["session_id"])
        if not replacement and stamp["session"]["agent_session_id"] != session_id:
            raise Conflict("old manifest Session requires explicit replace_session before execution")
        self._record_modules(actor["run_id"], actor["trial_id"])
        self.knowledge.before_effect(actor["run_id"], actor["trial_id"])
        check_actor(self.journal.snapshot(), actor, self.journal.clock())
        return actor, preparation

    def _bind(self, state: Json, *, replacement: bool = False) -> None:
        owner = self._managed_owner()
        actor, preparation = self._binding_inputs(state, replacement=replacement)
        if self.store is not None:
            if self.store.actor != actor:
                raise Conflict("one stream cannot replace an already-bound execution Actor")
            return
        store = RuntimeStore(self.journal, actor)
        task = Task.model_validate_json(canonical(preparation["task"]))
        workspace = self._workspace(actor)
        skills = Skills(tuple(self.base / root for root in self.config.skill_roots))
        binding = state["trial"]["external_snapshot"].get("knowledge")
        tools = self._tools(store, workspace, skills, self.knowledge.open(binding))
        writable_paths = (tuple(self.config.rsi["source_paths"]) if self.config.rsi is not None
                          else self.config.writable_paths)
        guards = (WorkspaceGuard(workspace.root, writable_paths, self.config.readonly_paths),
                  RequiredChecksGuard(store, task.id, self.config.required_checks),
                  *((SourceGuard(self.config.rsi["source_paths"]),) if self.config.rsi is not None else ()))
        gateway = Gateway(store, tools, task.scope, guards)
        specifications = gateway.specifications()
        actual_digest = "sha256:" + digest([spec.model_dump(mode="json") for spec in specifications])
        if ([spec.name for spec in specifications] != owner.tool_names or
                actual_digest != owner.version_lock["lock"]["tool_schema"]["value"]):
            raise Conflict("bound tools differ from the exact prepared catalogue")
        self.store, self.workspace, self.gateway = store, workspace, gateway
        self.selected_trial_id = actor["trial_id"]
        if state["stamp"]["run_status"] == "active":
            rsi = None if self.config.rsi is None else RsiEffects(gateway, workspace,
                self.artifacts, (self.base / self.config.state_dir).resolve(), task.id, self.config.rsi)
            self.host = ExecutionSession(self.gateway, _build_proposers(self.config.providers), skills.catalog(),
                child_proposers=_build_proposers(self.config.providers, child=True),
                profile=self.config.agent_profile, options=self.config.agent_options,
                task_identity=digest(task), external_context=True, cwd=self.workspace.root,
                role=self.config.pi_worker.role, timeout=self.config.pi_worker.timeout,
                max_frame_bytes=self.config.pi_worker.max_frame_bytes, rsi=rsi,
                workspace_policy={"root": str(workspace.root), "writable_paths": writable_paths,
                                  "readonly_paths": self.config.readonly_paths,
                                  "codemode": self.config.pi_worker.codemode},
                replay=NativeReplayFiles(store, (self.base / self.config.state_dir).resolve(), workspace.root, task.id,
                    seed=self.config.native_session_seed.model_dump(mode="json") if self.config.native_session_seed else None))
            self.handler = self.stack.enter_context(self.host.open_rpc(task))

    def _bind_finalization(self, params: Json) -> Json:
        if self.store is not None:
            raise Conflict("bind_finalization requires an unbound connection")
        expected = validate_runtime(params, "BindFinalizationRequest")["expected"]
        preparation = self._verified_preparation(expected["run_id"], expected["trial_id"])
        self._record_modules(expected["run_id"], expected["trial_id"])
        self.knowledge.before_effect(expected["run_id"], expected["trial_id"])
        store = RuntimeStore(self.journal, actor_from(expected))
        task = Task.model_validate_json(canonical(preparation["task"]))
        with ExitStack() as binding:
            binding.enter_context(store.task_lock(task.id))
            state = self.journal.bind_finalization(expected)
            self.workspace = self._workspace(store.actor)
            self.gateway = Gateway(store, [], task.scope)
            self.store = store
            self.selected_trial_id = expected["trial_id"]
            self.stack.enter_context(binding.pop_all())
        return state

    def _prepare_run(self, params: Json) -> Json:
        owner = self._managed_owner()
        request = validate_runtime(params, "PrepareRunRequest")
        if request["manifest"] != self.manifest or request["manifest_digest"] != self.manifest_digest:
            raise Conflict("PrepareRunRequest differs from actual active manifest bytes/path")
        prepared = owner.prepare(request)
        assert self.manifest is not None
        existing = prepared.get("existing_state")
        trial_id: str = self.manifest["trial_id"] if existing is None else existing["trial"]["trial_id"]
        self._select_trial(trial_id)
        self._record_modules(self.manifest["run_id"], trial_id)
        prepared = owner.prepare(request)
        if existing is not None:
            self._verified_preparation(self.manifest["run_id"], trial_id)
            if self.knowledge.application is not None:
                self.knowledge.sync(self.manifest["run_id"], trial_id)
        else:
            self.knowledge.install(owner, self.manifest["run_id"], self.manifest["trial_id"], prepared)
        prepared = owner.prepare(request)
        if "existing_state" in prepared:
            # Another connection may advance the Run while fence delivery is in flight.
            trial_id = prepared["existing_state"]["trial"]["trial_id"]
            self._verified_preparation(self.manifest["run_id"], trial_id)
            self._select_trial(trial_id)
        return prepared

    def _prepare_trial(self, params: Json) -> Json:
        owner = self._managed_owner()
        if self.store is not None:
            raise Conflict("prepare_trial requires an unbound connection; one stream cannot switch Actor")
        request = validate_runtime(params, "PrepareTrialRequest")
        assert self.manifest is not None
        if request["manifest_digest"] != self.manifest_digest or request["expected"]["run_id"] != self.manifest["run_id"]:
            raise Conflict("Trial preparation differs from the active connection manifest Run")
        self._verified_preparation(request["expected"]["run_id"], request["expected"]["trial_id"])
        prepared = owner.prepare_trial(request)
        self.selected_trial_id = request["trial_id"]
        self._record_modules(self.manifest["run_id"], request["trial_id"])
        prepared = owner.prepare_trial(request)
        if "existing_state" in prepared:
            return prepared
        self.knowledge.install(owner, self.manifest["run_id"], request["trial_id"], prepared)
        return owner.prepare_trial(request)

    def _freeze_run(self, params: Json) -> Json:
        owner = self._managed_owner()
        request = validate_runtime(params, "FreezeRunRequest")
        assert self.manifest is not None
        if request["intent_id"] != self.manifest["intent_id"] or request["manifest_digest"] != self.manifest_digest:
            raise Conflict("freeze request differs from the active connection manifest")
        self._manifest_preparation(self.manifest["run_id"], self.manifest["trial_id"])
        self._record_modules(self.manifest["run_id"], self.manifest["trial_id"])
        if self.knowledge.application is not None:
            self.knowledge.sync(self.manifest["run_id"], self.manifest["trial_id"])
        state = owner.freeze(request)
        if not state.get("freeze_replayed"):
            current = self.journal.state(state["run"]["run_id"], state["trial"]["trial_id"])
            if current["stamp"]["run_status"] == "active":
                self._bind(state)
        self._select_trial(state["trial"]["trial_id"])
        return state

    def _freeze_trial(self, params: Json) -> Json:
        owner = self._managed_owner()
        if self.store is not None:
            raise Conflict("freeze_trial requires an unbound connection; one stream cannot switch Actor")
        request = validate_runtime(params, "FreezeTrialRequest")
        assert self.manifest is not None
        if self.selected_trial_id is None:
            raise Conflict("freeze_trial requires the selected original Trial preparation")
        preparation = self._manifest_preparation(self.manifest["run_id"], self.selected_trial_id)
        prepared = preparation["prepared"]
        if prepared["schema_version"] != "kernelcat.prepared-trial/v1" or (
                request["intent_id"] != prepared["request"]["intent_id"] or request["request_digest"] != prepared["request_digest"]):
            raise Conflict("freeze_trial differs from the selected original Trial preparation")
        self._record_modules(self.manifest["run_id"], self.selected_trial_id)
        if self.knowledge.application is not None:
            self.knowledge.sync(self.manifest["run_id"], self.selected_trial_id)
        state = owner.freeze_trial(request)
        if not state.get("freeze_replayed") and state["stamp"]["run_status"] == "active":
            self._bind(state)
        return state

    def handle(self, method: str, params: Json) -> Json:
        if method == "account_create":
            if self.manifest is not None or self.store is not None:
                raise Conflict("account_create is a separate explicit bootstrap operation")
            request = validate_runtime(params, "AccountCreateRequest")
            self.knowledge.validate_creation(request)
            account = self.journal.create_account(request)
            self._record_modules()
            return account
        if method == "knowledge_fence":
            if self.manifest is not None or self.store is not None:
                raise Conflict("knowledge_fence requires a separate administrative bootstrap connection")
            self._record_modules()
            return dict(self.journal.knowledge_fence(params))
        if method == "knowledge_sync":
            if "limit" in params and type(params["limit"]) is not int:
                raise ValueError("Knowledge sync limit must be a strict integer")
            request = validate_runtime(params, "KnowledgeSyncRequest")
            self._record_modules()
            return dict(self.knowledge.sync(request["run_id"], request["trial_id"], limit=request.get("limit", 100)))
        if method == "prepare_run":
            return self._prepare_run(params)
        if method == "prepare_trial":
            return self._prepare_trial(params)
        if method == "freeze_run":
            return self._freeze_run(params)
        if method == "freeze_trial":
            return self._freeze_trial(params)
        if method == "runtime_state":
            request = validate_runtime(params, "RuntimeStateRequest")
            if self.manifest is not None:
                self._manifest_preparation(request["run_id"], request["trial_id"])
            return self.journal.state(request["run_id"], request["trial_id"])
        if method == "bind_finalization":
            return self._bind_finalization(params)
        if method == "managed_input":
            if self.host is None or self.handler is None or self.store is None:
                raise Conflict("managed_input requires this connection's bound execution Session")
            task_id = self.host.state.task_id
            if task_id is None:
                raise Conflict("managed_input has no bound task")
            return dict(enqueue_managed_input(params, task_id=task_id, control=self.host._control,
                                              store=self.store, knowledge=self.knowledge))
        if method == "replace_session":
            self._managed_owner()
            if self.store is not None:
                raise Conflict("replace_session requires an unbound bootstrap connection")
            request = validate_runtime(params, "ReplaceSessionRequest")
            self._verified_preparation(request["expected"]["run_id"], request["expected"]["trial_id"])
            self._record_modules(request["expected"]["run_id"], request["expected"]["trial_id"])
            self.knowledge.before_effect(request["expected"]["run_id"], request["expected"]["trial_id"])
            state = self.journal.control(request, replace=True)
            if state["stamp"]["run_status"] == "active":
                self._bind(state, replacement=True)
            return state
        if method == "stop_run":
            request = validate_runtime(params, "StopRunRequest")
            self._record_modules()
            return self.journal.control(request, replace=False)
        if method == "generation_result":
            if params:
                raise Conflict("generation_result takes no parameters")
            if self.handler is None or self.store is None:
                raise Conflict("generation_result has no frozen execution Session")
            if self.generation is not None:
                return self.generation
            self.knowledge.before_effect(self.store.actor["run_id"], self.store.actor["trial_id"])
            check_actor(self.journal.snapshot(), self.store.actor, self.journal.clock())
            result = self.handler("complete", {})
            self.journal.generation_stopped(self.store.actor)
            self.generation = result
            return result
        if method == "snapshot_program":
            owner = self._managed_owner()
            if self.store is None or self.gateway is None or self.workspace is None:
                raise Conflict("snapshot requires a currently bound frozen Actor")
            self.knowledge.before_effect(self.store.actor["run_id"], self.store.actor["trial_id"])
            materialize = partial(workspace_bundle, self.workspace)
            if self.config.rsi is not None:
                preparation = owner.preparation(self.store.actor["run_id"], self.store.actor["trial_id"])
                records = Records(self.store, self.artifacts, preparation["task"]["id"])
                materialize = partial(retained_bundle, self.workspace, records=records,
                                      source_paths=self.config.rsi["source_paths"])
            elif self.config.candidate_retention is not None:
                preparation = owner.preparation(self.store.actor["run_id"], self.store.actor["trial_id"])
                materialize = partial(retained_business_bundle, self.workspace, store=self.store,
                    artifacts=self.artifacts, task_id=preparation['task']['id'], retention=self.config.candidate_retention)
            return owner.snapshot(params, self.store, self.gateway, materialize)
        if method == "adopt_candidate":
            owner = self._managed_owner()
            if self.store is None:
                raise Conflict("adoption requires a currently bound frozen Actor")
            self.knowledge.before_effect(self.store.actor["run_id"], self.store.actor["trial_id"])
            return owner.adopt(params, self.store)
        if method == "complete":
            return self._complete(params)
        if self.handler is None or self.store is None:
            raise Conflict("freeze_run or explicit current replacement is required before Pi effects")
        # Only a permit retained by this original RPC can settle after revocation.
        # Both settlement methods verify their original permit/body before resolving it.
        if method not in {"native_settle", "plugin_settle"}:
            self.knowledge.before_effect(self.store.actor["run_id"], self.store.actor["trial_id"])
            check_actor(self.journal.snapshot(), self.store.actor, self.journal.clock())
        return self.handler(method, params)

    def _complete(self, params: Json) -> Json:
        if params:
            raise Conflict("complete takes no parameters")
        if self.manifest is None:
            return {"status": "bootstrap_complete"}
        assert self.selected_trial_id is not None
        self._record_modules(self.manifest["run_id"], self.selected_trial_id)
        if self.knowledge.application is not None:
            self.knowledge.sync(self.manifest["run_id"], self.selected_trial_id)
        state = self.journal.state(self.manifest["run_id"], self.selected_trial_id)
        pending = bool(state["pending_effect_ids"])
        invalidated = state.get("knowledge_invalidated", False)
        if (self.store is not None and self.handler is not None and self.generation is None
                and not pending and not invalidated):
            raise Conflict("generation_result must precede managed final completion")
        result = self.generation
        if result is None:
            result = self._recorded_generation(self.manifest["run_id"], self.selected_trial_id)
        if result is None:
            result = self._unknown_generation(pending=pending, invalidated=invalidated)
        if self.store is not None and not pending:
            state = self.journal.finalize(self.store.actor)
        return {**result, "runtime_state": state,
            "settlement": "pending" if state["pending_effect_ids"] else "settled",
            "evaluation": {"state": "unknown", "reason": "candidate bytes do not establish correctness or timing"}}

    def _recorded_generation(self, run_id: str, trial_id: str) -> Json | None:
        # Checkpoint reads grant no Actor and never borrow another Trial's result.
        task_id = self._manifest_preparation(run_id, trial_id)["task"]["id"]
        ends = [item["outcome"]["data"] for item in self.journal.snapshot()["actions"].values()
            if item["action"]["task"] == task_id and item["action"]["tool"] == "__turn_end__"
            and item.get("binding", {}).get("trial_id") == trial_id
            and item.get("terminal") and item["outcome"]["status"] == "completed"
            and item["outcome"]["data"]["result"] is not None]
        if not ends:
            return None
        result: Json = max(ends, key=lambda value: value["step"])["result"]
        return result

    @staticmethod
    def _unknown_generation(*, pending: bool, invalidated: bool) -> Json:
        if pending:
            status = "pending"
            reason = "original effects remain unresolved; no known generation result"
        elif invalidated:
            status = "stopped"
            reason = "Knowledge dependency permanently invalidated; no known generation result"
        else:
            status = "unknown"
            reason = "no known original generation result for the selected Trial"
        return {"status": status, "generation": {"state": "unknown", "reason": reason}}

    def close(self) -> None:
        self.stack.close()
        if self.host is not None:
            self.host.close()
