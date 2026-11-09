"""Structural module binding/report validation; startup admission has its owner."""

from __future__ import annotations

from typing import cast

from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.generated.python_modules import (
    SCHEMA,
    ExpectedPythonModules,
    PythonModuleReport,
)


def _snapshot(value: object, definition: str) -> object:
    schema = {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]}
    return validated_snapshot(value, Draft202012Validator(schema), definition)


def validate_expected_python_modules(value: object) -> ExpectedPythonModules:
    """Copy wire shape only; do not observe modules or authorize their content."""
    return cast(ExpectedPythonModules, _snapshot(value, "ExpectedPythonModules"))


def validate_python_module_report(value: object) -> PythonModuleReport:
    """A known origin is an observation, not a matching/admitted implementation."""
    return cast(PythonModuleReport, _snapshot(value, "PythonModuleReport"))
