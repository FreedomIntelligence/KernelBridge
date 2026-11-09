"""Finite wire validation for lab object snapshots, without ownership decisions."""
from __future__ import annotations

from typing import cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.generated.knowledge_runtime import SCHEMA as KNOWLEDGE_SCHEMA
from kernelcat_executor.generated.objects import SCHEMA, ObjectGraph
from kernelcat_executor.generated.task_contract import SCHEMA as TASK_SCHEMA
from kernelcat_executor.generated.version_lock import SCHEMA as VERSION_SCHEMA

_REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema))
    for schema in (TASK_SCHEMA, VERSION_SCHEMA, KNOWLEDGE_SCHEMA)
)
_VALIDATOR = Draft202012Validator(SCHEMA, registry=_REGISTRY)


def validate_object_graph(value: object) -> ObjectGraph:
    """Detach and validate supplied wire data; do not authenticate or adopt it."""
    return cast(ObjectGraph, validated_snapshot(value, _VALIDATOR, "ObjectGraph"))
