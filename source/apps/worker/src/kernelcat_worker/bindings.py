"""Composition root: configuration is operator authority, never model output."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.llm import APIProposer
from kernelcat_executor.capabilities.process import CommandEvaluator
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.capabilities.skills import Skills
from kernelcat_executor.capabilities.workspace_tools import builtin_tools
from kernelcat_executor.capabilities.workspace_policy import WorkspaceGuard
from kernelcat_executor.contracts.models import Conflict, Json, Task, digest
from kernelcat_executor.contracts.ports import Proposer, Tool
from kernelcat_executor.contracts.providers import validate_endpoint
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.hooks import OutcomeObserver
from kernelcat_executor.operations.registry import ExecutionGuard
from kernelcat_executor.operations.session import ExecutionSession
from kernelcat_executor.profiles.evidence import ReviewExperience
from kernelcat_executor.profiles.promotion import PromoteOnFinish
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_executor.profiles.tools import (
    ProposeImprovement,
    ReadLearnedSkill,
    ValidateImprovement,
)

from kernelcat_worker.configuration import Config, PiWorkerConfig, ProviderConfig
from kernelcat_worker.replay import NativeReplayFiles
from kernelcat_worker.rsi_adapter import check_configuration
from kernelcat_worker.rsi_branches import BranchGuard, branch_workspace
from kernelcat_worker.rsi_effects import RsiEffects
from kernelcat_worker.rsi_experiments import ExperimentTool, SourceGuard
from kernelcat_worker.rsi_records import Records
from kernelcat_worker.rsi_replay import ReplayTool, pending_request, replay_plugin
from kernelcat_worker.startup import PythonStartup, admit_config
from kernelcat_worker.versions import VersionCapture, collect_version_lock, runtime_inputs


def _check_executable(argv: tuple[str, ...], label: str) -> None:
    executable = Path(argv[0]) if argv else Path("")
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"{label} executable must be an existing executable absolute path")


def _file_hashes(argv: tuple[str, ...]) -> dict[str, str]:
    hashes = {}
    for argument in argv:
        candidate = Path(argument)
        if candidate.is_absolute() and candidate.is_file():
            with candidate.open("rb") as stream:
                hashes[argument] = hashlib.file_digest(stream, "sha256").hexdigest()
    return hashes


def _knowledge_root(config: Config, base: Path) -> Path | None:
    if config.knowledge_root is None:
        return None
    root = (base / config.knowledge_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Knowledge resource root must be an existing directory")
    for name in (config.workspace, config.state_dir):
        writable = (base / name).resolve()
        if root.is_relative_to(writable) or writable.is_relative_to(root):
            raise ValueError("Knowledge resource root and writable state/workspace must be disjoint")
    return root


def _pi_files(worker: PiWorkerConfig, base: Path, writable: Path, *, project: bool = False) -> tuple[Path, ...]:
    _check_executable((worker.node,), "Pi node")
    root = (base / worker.root).resolve()
    if not project and (root.is_relative_to(writable) or writable.is_relative_to(root)):
        raise ValueError("Pi worker source and writable workspace must be disjoint")
    expected = {
        "@earendil-works/pi-agent-core": "1.5.0",
        "@earendil-works/pi-ai": "1.5.0",
        "@earendil-works/pi-coding-agent": "1.5.0",
        "@earendil-works/pi-tui": "1.5.0",
        "ajv": "8.17.1",
        "pi-subagents": "0.87.0",
        "pi-workspace-history": "0.5.2",
        "typebox": "1.3.27",
    }
    files = (root / "package.json", root / "package-lock.json",
             *(root / "src" / p for p in ("harness/pi/worker.mjs", "clients/protocol.mjs", "harness/pi/sdk.mjs", "harness/pi/plugin.mjs", "harness/pi/child-tools.mjs")),
             *(root / "node_modules" / n / "package.json" for n in expected))
    if any(not p.is_file() for p in files):
        raise ValueError("Pi SDK/subagents are required; explicitly install the locked worker first")
    if not project and any(p.resolve().is_relative_to(writable) for p in files):
        raise ValueError("Pi worker files cannot resolve inside the writable workspace")
    package, lock = (json.loads(files[i].read_text()) for i in (0, 1))
    if (package.get("dependencies") != expected or lock.get("lockfileVersion") != 3
            or lock.get("packages", {}).get("", {}).get("dependencies") != expected):
        raise ValueError("Pi dependency lock does not match the reviewed package versions")
    for name, wanted in expected.items():
        metadata = json.loads((root / "node_modules" / name / "package.json").read_text())
        entry = lock["packages"].get("node_modules/" + name, {})
        if metadata.get("name") != name or metadata.get("version") != wanted or entry.get("version") != wanted:
            raise ValueError("installed Pi dependency differs from reviewed version")
    return files


def _build_proposers(
    providers: Mapping[str, ProviderConfig], *, child: bool = False
) -> dict[str, Proposer]:
    """Construct transports without sending requests or mutating operator policy."""
    result: dict[str, Proposer] = {}
    for route, provider in providers.items():
        wire_options = provider.wire_options
        if child:
            wire_options = wire_options.model_copy(
                update={"native_turns": True, "tool_choice": "auto"}
            )
        result[route] = APIProposer(
            provider.provider,
            provider.base_url,
            provider.model,
            os.environ.get(provider.api_key_env, ""),
            provider.max_tokens,
            provider.timeout,
            wire_options=wire_options,
            allow_insecure_http=provider.allow_insecure_http,
            max_response_bytes=provider.max_response_bytes,
        )
    return result


def _version_inputs(config: Config, base: Path, skills: Skills) -> Json:
    writable = (base / config.workspace).resolve()
    files = _pi_files(config.pi_worker, base, writable, project=config.workspace_mode == "project")
    root = (base / config.pi_worker.root).resolve()
    sources = tuple(path for path in sorted((root / "src").rglob("*"))
                    if path.suffix in {".mjs", ".ts"} and path.is_file())
    entry = root / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    bootstrap = root / "python/generated_startup.py"
    if not bootstrap.is_file():
        raise ValueError("reviewed Agent Python bootstrap is required")
    admitted = (*files, *sources, bootstrap, *((entry,) if entry.is_file() else ()))
    if config.workspace_mode != "project" and any(path.resolve().is_relative_to(writable) for path in admitted):
        raise ValueError("Pi runtime files cannot resolve inside the writable workspace")
    validator = config.self_evolution.validator if config.self_evolution else None
    return runtime_inputs(config, skills.fingerprint(),
        evaluator_files={**(_file_hashes(config.evaluator.argv) if config.evaluator else {}),
            **{path: value for tool in config.business_tools for path, value in
               _file_hashes((*tool.argv, *(item.path for item in tool.files))).items()}},
        validator_files=_file_hashes(validator.argv) if validator else {},
        worker_files=_file_hashes((config.pi_worker.node, *map(str, admitted))))


def observe_versions(config: Config, base: Path, *,
                     startup: PythonStartup | None = None) -> VersionCapture:
    """Read-only local observation, without credentials, state or tool construction."""
    startup = admit_config(config) if startup is None else startup
    config = startup.bind_config(config)
    skills = Skills(tuple(base / root for root in config.skill_roots))
    inputs = _version_inputs(config, base, skills)
    return collect_version_lock(config, inputs, (base / config.pi_worker.root).resolve())


def bind_retained_runtime(config: Config, base: Path, store: AgentJournal,
                          startup: PythonStartup) -> None:
    """Recheck current input bytes against the original lock for late collection.

    Collection starts no provider, tool registry or new executable observation.
    Reusing the immutable lock retains its original provenance; the original
    Journal still compares all current configuration/resource/runtime inputs.
    """
    config = startup.bind_config(config)
    if "KERNELCAT_MAX_FRAME_BYTES" in os.environ:
        from kernelcat_worker.compose import _active_transport

        config = _active_transport(config, os.environ)
    prior = store.runtime_binding()
    if prior is None or "version_lock" not in prior:
        raise Conflict("RSI collection requires the original structured runtime binding")
    skills = Skills(tuple(base / root for root in config.skill_roots))
    inputs = _version_inputs(config, base, skills)
    store.bind_runtime(inputs=inputs, version_lock=prior["version_lock"],
                       probe_outputs=prior["probe_outputs"])


def preflight(config: Config, base: Path, *, startup: PythonStartup | None = None) -> Json:
    """Offline checks only: no model request, runner launch, or state creation."""
    startup = admit_config(config) if startup is None else startup
    return _preflight(startup.bind_config(config), base)


def _preflight(config: Config, base: Path) -> Json:
    _knowledge_root(config, base)
    if config.rsi is not None:
        check_configuration(config.rsi, (base / config.workspace).resolve(),
                            (base / config.state_dir).resolve())
        if config.pi_worker.role != "coder":
            raise ValueError("RSI is a parent coder extension")
        if config.rsi["knowledge_query"] is not None and config.knowledge_root is None:
            raise ValueError("RSI knowledge query requires an explicit Knowledge owner")
    for route in config.policy.routes:
        if route not in config.providers:
            raise ValueError(f"unconfigured model route: {route}")
    for provider in config.providers.values():
        validate_endpoint(provider.base_url, provider.allow_insecure_http)
        if not provider.model.strip() or provider.model.startswith(("REPLACE_", "SELECT_")):
            raise ValueError("configure an explicit authorized model ID")
        if not os.environ.get(provider.api_key_env, "").strip():
            raise ValueError(f"missing provider credential environment variable: {provider.api_key_env}")
    _pi_files(config.pi_worker, base, (base / config.workspace).resolve(), project=config.workspace_mode == "project")
    skills = Skills(tuple(base / r for r in config.skill_roots))
    if config.evaluator:
        _check_executable(config.evaluator.argv, "evaluator")
        try:
            Draft202012Validator.check_schema(config.evaluator.parameters)
        except SchemaError:
            raise ValueError("invalid evaluator JSON Schema") from None
    evolution = config.self_evolution
    if evolution is not None:
        known = {"read_file", "load_skill", "read_receipt"}
        if config.evaluator:
            known.add("evaluate")
        if (len(set(evolution.evidence_tools)) != len(evolution.evidence_tools)
                or not set(evolution.evidence_tools).issubset(known)):
            raise ValueError("evolution evidence_tools must name available observation capabilities")
        if evolution.validator is not None:
            _check_executable(evolution.validator.argv, "validator")
            writable = (base / config.workspace).resolve()
            for arg in evolution.validator.argv:
                path = Path(arg)
                if path.is_absolute() and path.resolve().is_relative_to(writable):
                    raise ValueError("validator files must be outside the model-writable workspace")
    return {"status": "ready", "skills": len(skills.catalog()),
            "skill_fingerprint": skills.fingerprint(), "routes": list(config.policy.routes),
            "evaluator_configured": config.evaluator is not None, "network_checked": False,
            "runtime_engine": "pi",
            "agent_evolution": ("disabled" if evolution is None else
                                "validated" if evolution.validator is not None else "proposal_only"),
            "insecure_http_routes": [r for r, p in config.providers.items()
                                     if p.base_url.startswith("http://") and p.allow_insecure_http]}


def build(config: Config, base: Path, task: Task, *,
          guards: tuple[ExecutionGuard, ...] = (),
          extra_tools: tuple[Tool, ...] = (),
          probe_versions: bool = True, external_context: bool = False,
          startup: PythonStartup | None = None) -> ExecutionSession:
    startup = admit_config(config) if startup is None else startup
    config = startup.bind_config(config)
    _preflight(config, base)
    if config.pi_worker.role == "scout" and task.seed_files:
        raise ValueError("read-only scouts cannot seed a writable workspace")
    initial_workspace = (base / config.workspace).resolve()
    if config.workspace_mode != "project":
        initial_workspace /= digest_task(task)
    elif task.seed_files or config.rsi or config.evaluator or config.business_tools:
        raise ValueError("project codegen cannot seed files or implicitly enable experiment tools; use an isolated task/profile")
    check_workspace_paths(config, base, initial_workspace)
    state_dir = (base / config.state_dir).resolve()
    workspace_path = selected_workspace(config, base, task)
    check_workspace_paths(config, base, workspace_path)
    skills = Skills(tuple(base / r for r in config.skill_roots))
    tools: list[Tool] = []
    knowledge_root = _knowledge_root(config, base)
    if knowledge_root is not None:
        from kernelcat_knowledge import build_application

        from kernelcat_worker.knowledge import knowledge_tools

        tools.extend(knowledge_tools(build_application(
            knowledge_root, expected_modules=startup.knowledge_expected)))
    workspace = Workspace(workspace_path)
    writable_paths = (tuple(config.rsi["source_paths"]) if config.rsi is not None
                      else config.writable_paths)
    from kernelcat_worker.candidate_retention import RequiredChecksGuard
    store = AgentJournal(state_dir / "events.jsonl")
    guards = (*guards, WorkspaceGuard(workspace.root, writable_paths, config.readonly_paths),
              RequiredChecksGuard(store, task.id, config.required_checks))
    store.initialize(config.credits, config.policy)
    artifacts = RuntimeArtifacts(state_dir / "runtime-artifacts")
    records = Records(store, artifacts, task.id)
    validator_config = config.self_evolution.validator if config.self_evolution else None
    inputs = _version_inputs(config, base, skills)
    validator_hashes = inputs["validator_files"]
    tools.extend([*builtin_tools(workspace, skills), SubagentEffect()])
    from kernelcat_worker.business_tools import configured_tools
    tools.extend(configured_tools(config.business_tools, workspace, state_dir))
    if config.rsi is not None:
        tools.append(ExperimentTool(store, workspace, artifacts, config.rsi))
        if replay_plugin(config.rsi) is not None:
            tools.append(ReplayTool(records))
        guards = (*guards, SourceGuard(config.rsi["source_paths"]), BranchGuard(records, config.rsi["source_paths"]))
    if config.evaluator:
        e = config.evaluator
        env = {key: os.environ[key] for key in e.env_allowlist if key in os.environ}
        tools.append(CommandEvaluator(e.argv, workspace, e.parameters, e.credits, e.timeout, env,
                                      artifact_root=state_dir / "evaluations"))
    observers: list[OutcomeObserver] = []
    tools.append(ReadLearnedSkill(store))
    if config.self_evolution is not None:
        tools.append(ReviewExperience(store, config.self_evolution.evidence_tools))
        tools.append(ProposeImprovement(store, config.self_evolution.evidence_tools))
        if validator_config is not None:
            v = validator_config
            validator = CommandEvaluator(v.argv, Workspace(state_dir / "validator-workspaces" / digest_task(task)),
                {"type": "object"}, v.credits, v.timeout,
                {key: os.environ[key] for key in v.env_allowlist if key in os.environ},
                artifact_root=state_dir / "agent-validations")
            tools.append(ValidateImprovement(store, validator,
                digest({"config": v.model_dump(mode="json"), "files": validator_hashes})))
            observers.append(PromoteOnFinish())
    providers = _build_proposers(config.providers)
    child_providers = _build_proposers(config.providers, child=True)
    gateway = Gateway(store, [*tools, *extra_tools], task.scope, guards)
    rsi = None if config.rsi is None else RsiEffects(
        gateway, workspace, artifacts, state_dir, task.id, config.rsi)
    tool_schema_digest = "sha256:" + digest([
        spec.model_dump(mode="json") for spec in gateway.specifications()])
    capture = collect_version_lock(config, inputs, (base / config.pi_worker.root).resolve(),
                                   tool_schema_digest=tool_schema_digest,
                                   probe_executables=probe_versions)
    store.bind_runtime(inputs=inputs, version_lock=capture.lock, probe_outputs=capture.probe_outputs)
    store.record_python_modules(startup.expected, startup.report)
    worker = config.pi_worker
    script = (base / worker.root).resolve() / "src/harness/pi/worker.mjs"
    return ExecutionSession(gateway, providers, skills.catalog(), argv=(worker.node, str(script)),
        cwd=workspace_path, timeout=max(worker.timeout, config.evaluator.timeout + 30 if config.evaluator else 0,
            *(tool.timeout + 30 for tool in config.business_tools)),
        max_frame_bytes=worker.max_frame_bytes,
        workspace_policy={"root": str(workspace.root), "writable_paths": writable_paths,
                          "readonly_paths": config.readonly_paths, "codemode": worker.codemode,
                          "mode": config.workspace_mode, "home": str(Path.home())},
        role=worker.role, child_proposers=child_providers, profile=config.agent_profile,
        observers=tuple(observers), task_identity=digest(task), options=config.agent_options,
        external_context=external_context, rsi=rsi,
        should_stop_after_turn=(lambda _state: pending_request(records) is not None)
            if replay_plugin(config.rsi) is not None else None,
        replay=NativeReplayFiles(store, state_dir, workspace.root, task.id,
            plugin=replay_plugin(config.rsi),
            seed=config.native_session_seed.model_dump(mode="json") if config.native_session_seed else None)
            if external_context else None)


def digest_task(task: Task) -> str:
    return digest(task)[:20]


def check_workspace_paths(config: Config, base: Path, workspace: Path) -> None:
    """Reject overlapping owner roots before creating or funding a Journal."""
    state = (base / config.state_dir).resolve()
    if state.is_relative_to(workspace) or workspace.is_relative_to(state):
        raise ValueError("state and workspace must be disjoint directories")
    for root in config.skill_roots:
        resolved = (base / root).resolve()
        if resolved.is_relative_to(workspace) or workspace.is_relative_to(resolved):
            raise ValueError("skills and writable workspace must be disjoint")


def selected_workspace(config: Config, base: Path, task: Task) -> Path:
    if config.workspace_mode == "project":
        return (base / config.workspace).resolve(strict=True)
    state = (base / config.state_dir).resolve()
    initial = (base / config.workspace).resolve() / digest_task(task)
    store = AgentJournal(state / "events.jsonl")
    # A fresh task has no branch authority to read. Existing empty or malformed
    # ledgers still fail their original snapshot validation; they are not fresh.
    if not store.path.exists():
        return initial
    records = Records(store, RuntimeArtifacts(state / "runtime-artifacts"), task.id)
    return branch_workspace(records, initial)
