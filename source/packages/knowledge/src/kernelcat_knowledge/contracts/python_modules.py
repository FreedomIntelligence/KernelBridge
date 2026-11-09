"""Structural module binding/report validation without importing implementation I/O."""

from __future__ import annotations

import json
from typing import cast

from kernelcat_knowledge.contracts.runtime import validate_definition, wire_bytes
from kernelcat_knowledge.generated.python_modules import (
    SCHEMA,
    ExpectedPythonModules,
    PythonModuleReport,
)


def _snapshot(value: object, definition: str) -> object:
    snapshot = json.loads(wire_bytes(value))
    validate_definition(snapshot, SCHEMA, definition)
    return snapshot


def validate_expected_python_modules(value: object) -> ExpectedPythonModules:
    """Copy wire shape only; canonical paths and module selection need admission."""
    return cast(ExpectedPythonModules, _snapshot(value, "ExpectedPythonModules"))


def validate_python_module_report(value: object) -> PythonModuleReport:
    """Keep unknown explicit; this does not approve any observed implementation."""
    return cast(PythonModuleReport, _snapshot(value, "PythonModuleReport"))
