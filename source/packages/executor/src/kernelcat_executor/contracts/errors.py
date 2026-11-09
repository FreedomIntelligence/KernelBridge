"""Finite lab error declarations; existing transports keep their own adoption."""
from __future__ import annotations

from typing import cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.generated.errors import SCHEMA, LabErrorEnvelope
from kernelcat_executor.generated.task_contract import SCHEMA as TASK_SCHEMA

_REGISTRY = Registry().with_resource(TASK_SCHEMA["$id"], Resource.from_contents(TASK_SCHEMA))
_VALIDATOR = Draft202012Validator(SCHEMA, registry=_REGISTRY)


def validate_lab_error(value: object) -> LabErrorEnvelope:
    """Validate a detached envelope, without establishing outcome or retry safety."""
    return cast(LabErrorEnvelope, validated_snapshot(value, _VALIDATOR, "LabErrorEnvelope"))
