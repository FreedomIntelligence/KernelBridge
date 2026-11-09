"""Compose observed identities from the existing configuration/resource owners."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import kernelcat_executor
from kernelcat_executor.capabilities.versions import (
    known,
    observe_executable,
    observe_source,
    unknown,
)
from kernelcat_executor.contracts.models import Json, canonical, digest
from kernelcat_executor.contracts.version_lock import validate_version_lock
from kernelcat_executor.generated.version_lock import (
    ExecutableVersions,
    IdentityObservation,
    ModelIdentity,
    PackageIdentity,
    VersionLock,
)

from kernelcat_worker.configuration import Config


@dataclass(frozen=True)
class VersionCapture:
    lock: VersionLock
    probe_outputs: Json


def runtime_inputs(config: Config, resource_fingerprint: str, *, evaluator_files: Json,
                   validator_files: Json, worker_files: Json) -> Json:
    """Retain all pre-A02 fingerprint inputs at their original composition owner."""
    roots = {"executor": Path(kernelcat_executor.__file__).parent,
             "worker": Path(__file__).parent}
    if config.knowledge_root is not None:
        import kernelcat_knowledge

        roots["knowledge"] = Path(kernelcat_knowledge.__file__).parent
    sources = {owner + "/" + path.relative_to(folder).as_posix():
               hashlib.sha256(path.read_bytes()).hexdigest()
               for owner, folder in roots.items()
               for path in sorted(folder.rglob("*.py")) if not path.is_symlink()}
    return {"config": config.model_dump(mode="json"), "sources": sources,
            "skills": resource_fingerprint, "evaluator_files": evaluator_files,
            "validator_files": validator_files, "pi_worker_files": worker_files,
            "python": sys.version,
            "dependencies": {name: version(name)
                             for name in ("pydantic", "httpx", "PyYAML", "jsonschema")}}


def _read_metadata(path: Path, inputs: Json) -> Json:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != inputs["pi_worker_files"].get(str(path)):
        raise ValueError("Pi metadata changed after runtime admission")
    metadata: Json = json.loads(data)
    return metadata


def _packages(root: Path, inputs: Json) -> dict[str, PackageIdentity]:
    manifest = _read_metadata(root / "package.json", inputs)
    packages: dict[str, PackageIdentity] = {}
    for name, constraint in manifest["dependencies"].items():
        path = root / "node_modules" / name / "package.json"
        installed = _read_metadata(path, inputs)
        packages[name] = {"declared_constraint": constraint,
            "installed": known(installed["version"], "installed_metadata",
                               "sha256:" + inputs["pi_worker_files"][str(path)])}
    return packages


def _models(config: Config) -> dict[str, ModelIdentity]:
    if not config.providers:
        raise ValueError("version collection requires at least one explicit requested model route")
    return {route: {"requested_provider": provider.provider, "requested_model": provider.model,
        "parameters_digest": "sha256:" + digest(provider.model_dump(mode="json")),
        "reported_model": unknown("no provider response model identity observed; collection sends no request"),
        "resolved_revision": unknown("no model owner revision receipt observed")}
        for route, provider in config.providers.items()}


def _executables(config: Config, root: Path, inputs: Json) -> tuple[ExecutableVersions, Json]:
    files = inputs["pi_worker_files"]
    node_files = {config.pi_worker.node: "sha256:" + files[config.pi_worker.node]}
    node = observe_executable((config.pi_worker.node, "--version"), node_files, node=True)
    executables: ExecutableVersions = {"node": node.version,
        "pi": {"identity": unknown("reviewed installed Pi CLI entry was not available for observation")},
        "loopx": {"identity": unknown("no LoopX binding supplied to this composition")}}
    outputs: Json = {"node": node.outputs}
    entry = root / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    if str(entry) in files:
        pi = observe_executable((config.pi_worker.node, str(entry), "--offline", "--version"),
            {**node_files, str(entry): "sha256:" + files[str(entry)]})
        executables["pi"] = pi.version
        outputs["pi"] = pi.outputs
    return executables, outputs


def collect_version_lock(config: Config, inputs: Json, worker_root: Path, *,
                         tool_schema_digest: str | None = None,
                         probe_executables: bool = True,
                         probe_source: bool = True) -> VersionCapture:
    """Finite collection only; missing owner facts remain explicit unknowns."""
    models = _models(config)
    packages = _packages(worker_root, inputs)
    if probe_executables:
        executables, outputs = _executables(config, worker_root, inputs)
    else:
        reason = "Node-owned startup does not launch executable probes in the finite worker"
        executables = {"node": {"identity": unknown(reason)},
                       "pi": {"identity": unknown(reason)},
                       "loopx": {"identity": unknown(reason)}}
        outputs = {"executable_probes": {"status": "unknown", "reason": reason}}
    tool_schema: IdentityObservation = unknown("no sealed tool registry observed by stand-alone collection")
    if tool_schema_digest is not None:
        tool_schema = known(tool_schema_digest, "owner_receipt", tool_schema_digest)
    lock = validate_version_lock({
        "schema_version": "kernelcat.version-lock/v1",
        "runtime_inputs_digest": "sha256:" + digest(inputs),
        "configuration_digest": "sha256:" + digest(inputs["config"]),
        "resource_manifest_digest": "sha256:" + inputs["skills"],
        "source": (observe_source(Path(__file__).resolve(), "sha256:" + digest({
            "host": inputs["sources"], "worker": inputs["pi_worker_files"]})) if probe_source else
            unknown("Git observation not repeated; original preparation retains its provenance, "
                    "and current admitted bytes are bound by runtime_inputs_digest")),
        "host_runtime": known(inputs["python"], "runtime", "sha256:" + digest({
            "python": inputs["python"], "dependencies": inputs["dependencies"]})),
        "packages": packages, "executables": executables, "models": models,
        "rules": {name: unknown(f"v8 {name} owner identity not yet supplied")
                  for name in ("controller", "context_compiler", "prune_policy", "replay_rules")},
        "kb_snapshot": unknown("no Wiki owner snapshot receipt supplied"),
        "tool_schema": tool_schema,
        "native_environment": {name: unknown(f"native {name} identity not observed; no native probe run")
                               for name in ("compiler", "driver", "evaluation_image")},
    })
    return VersionCapture(lock, outputs)


def export_version_evidence(path: Path, capture: VersionCapture) -> None:
    """Explicit create-only report export; never a second campaign evidence store."""
    payload = canonical({"version_lock": capture.lock, "probe_outputs": capture.probe_outputs})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload + "\n")
        stream.flush()
        os.fsync(stream.fileno())
