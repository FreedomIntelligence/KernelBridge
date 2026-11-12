"""Local validation and canonical bytes for the finite managed-runtime boundary."""
from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass
from typing import cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.models import Conflict, Json, canonical, digest
from kernelcat_executor.generated.context import SCHEMA as CONTEXT_SCHEMA
from kernelcat_executor.generated.context import ContextSource
from kernelcat_executor.generated.harness import SCHEMA as HARNESS_SCHEMA
from kernelcat_executor.generated.knowledge_runtime import SCHEMA as KNOWLEDGE_SCHEMA
from kernelcat_executor.generated.objects import SCHEMA as OBJECT_SCHEMA
from kernelcat_executor.generated.rsi import SCHEMA as RSI_SCHEMA
from kernelcat_executor.generated.runtime import (
    SCHEMA,
    ContextCommit,
    ProgramBundle,
    RunLaunchManifest,
)
from kernelcat_executor.generated.runtime_control import SCHEMA as CONTROL_SCHEMA
from kernelcat_executor.generated.task_contract import SCHEMA as TASK_SCHEMA
from kernelcat_executor.generated.version_lock import SCHEMA as VERSION_SCHEMA

# No retrieval callback: unresolved references fail locally, never fetch a schema.
LOCAL_SCHEMAS = (SCHEMA, CONTROL_SCHEMA, OBJECT_SCHEMA, TASK_SCHEMA, VERSION_SCHEMA,
                 CONTEXT_SCHEMA, HARNESS_SCHEMA, KNOWLEDGE_SCHEMA, RSI_SCHEMA)
SCHEMA_REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in LOCAL_SCHEMAS)
PROGRAM_BYTES = 8 * 1024 * 1024
RUNTIME_BYTES = 16 * 1024 * 1024
RUNTIME_METHODS = frozenset({"account_create", "prepare_run", "freeze_run", "runtime_state",
    "replace_session", "stop_run", "bind_finalization", "generation_result", "snapshot_program", "adopt_candidate",
    "knowledge_fence", "prepare_trial", "freeze_trial", "knowledge_sync", "managed_input"})


def validate_runtime(value: object, name: str) -> Json:
    validator = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/" + name},
                                    registry=SCHEMA_REGISTRY)
    return cast(Json, validated_snapshot(value, validator, name))


def bytes_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def program_path(value: str) -> str:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("Program path must be relative POSIX UTF-8")
    normalized = posixpath.normpath(value)
    if normalized.startswith("../"):
        raise ValueError("Program path escapes its bundle")
    if normalized in {".", ".."} or normalized.split("/")[0] in {".git", ".kernelagent", ".evaluations"}:
        raise ValueError("Program path is empty or controller-reserved")
    normalized.encode("utf-8", errors="strict")
    return normalized


def normalize_program(value: object) -> ProgramBundle:
    bundle = cast(ProgramBundle, validate_runtime(value, "ProgramBundle"))
    paths: set[str] = set()
    for entry in bundle["files"]:
        entry["path"] = program_path(entry["path"])
        entry["text"].encode("utf-8", errors="strict")
        if entry["path"] in paths:
            raise ValueError("Program bundle has duplicate normalized paths")
        paths.add(entry["path"])
    bundle["files"].sort(key=lambda entry: entry["path"].encode("utf-8"))
    bundle["entrypoint"] = program_path(bundle["entrypoint"])
    if bundle["entrypoint"] not in paths:
        raise ValueError("Program entrypoint is absent from its bundle")
    if len(canonical(bundle).encode("utf-8")) > PROGRAM_BYTES:
        raise ValueError("Program bundle exceeds 8 MiB canonical UTF-8 bytes")
    return bundle


def validate_launch(value: object) -> RunLaunchManifest:
    manifest = cast(RunLaunchManifest, validate_runtime(value, "RunLaunchManifest"))
    normalize_program(manifest["program"])
    canonical(manifest).encode("utf-8", errors="strict")
    return manifest


@dataclass(frozen=True)
class ContextAdmission:
    """Trusted adapter inputs; this is internal, never another wire protocol."""
    commit: ContextCommit
    source: ContextSource
    context: ContextSource
    actual_input: Json
    # Trusted host capability, never accepted from a model/client request.
    concurrent_budget: bool = False


def require(actual: object, expected: object, label: str) -> None:
    if digest(actual) != digest(expected):
        raise Conflict("frozen runtime graph changed " + label)
