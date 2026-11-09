"""Finite VersionLock validation against the committed local schema registry."""
from __future__ import annotations

from typing import cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.models import Json
from kernelcat_executor.generated.task_contract import SCHEMA as TASK_SCHEMA
from kernelcat_executor.generated.version_lock import SCHEMA, UnknownIdentity, VersionLock

_REGISTRY = Registry().with_resource(TASK_SCHEMA["$id"], Resource.from_contents(TASK_SCHEMA))
_VALIDATOR = Draft202012Validator(SCHEMA, registry=_REGISTRY)


def validate_version_lock(value: object) -> VersionLock:
    """Return a detached snapshot; validation neither observes nor qualifies versions."""
    return cast(VersionLock, validated_snapshot(value, _VALIDATOR, "VersionLock"))


def managed_execution_identity(lock: VersionLock) -> Json:
    """Compare admitted inputs; Git metadata remains immutable audit evidence.

    runtime_inputs_digest always binds the actual source bytes, including when
    the optional Git observation is unknown. Keep every other owner field strict.
    This projection does not replace the full evidence digest or its wire schema.
    """
    return {key: value for key, value in lock.items() if key != "source"}


def unknown(reason: str) -> UnknownIdentity:
    return {"state": "unknown", "reason": reason}
