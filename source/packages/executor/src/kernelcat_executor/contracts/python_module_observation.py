"""Typed origin evidence; filesystem admission remains at the trusted startup owner."""
from __future__ import annotations

from copy import deepcopy
from typing import cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.models import digest
from kernelcat_executor.contracts.python_modules import (
    validate_expected_python_modules,
    validate_python_module_report,
)
from kernelcat_executor.generated.python_module_observation import (
    SCHEMA,
    PythonModuleObservationBinding,
    PythonModuleObservationRecord,
)
from kernelcat_executor.generated.python_modules import SCHEMA as MODULE_SCHEMA
from kernelcat_executor.generated.python_modules import ExpectedPythonModules, PythonModuleReport

EVENT_KIND = "python_modules_observed/v1"
RECORD_VERSION = "kernelcat.python-module-observation/v1"
_REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in (SCHEMA, MODULE_SCHEMA)
)
_VALIDATOR = Draft202012Validator(SCHEMA, registry=_REGISTRY)


def observation_evidence(
    expected_value: object, report_value: object,
) -> tuple[ExpectedPythonModules, PythonModuleReport]:
    """Detach and normalize already observed evidence, without inspecting an installation."""
    expected = validate_expected_python_modules(expected_value)
    report = validate_python_module_report(report_value)
    return _normalized_evidence(expected, report)


def _normalized_evidence(
    expected: ExpectedPythonModules, report: PythonModuleReport,
) -> tuple[ExpectedPythonModules, PythonModuleReport]:
    """Check semantics on schema-validated copies without changing retained ordering."""
    expected, report = deepcopy(expected), deepcopy(report)
    expected["modules"].sort(key=lambda item: item["module_name"])
    report["observations"].sort(key=lambda item: item["module_name"])
    for item in expected["modules"]:
        item["allowed_package_roots"].sort(key=lambda value: value.encode("utf-8"))
    names = [item["module_name"] for item in expected["modules"]]
    observed_names = [item["module_name"] for item in report["observations"]]
    if len(set(names)) != len(names) or observed_names != names:
        raise ValueError("module observation requires one report for each expected module")
    if report["expected_binding_digest"] != "sha256:" + digest(expected):
        raise ValueError("module observation expected-binding digest differs")
    for item, observation in zip(expected["modules"], report["observations"], strict=True):
        origin = observation["origin"]
        if origin["state"] != "known":
            raise ValueError("unknown module origin cannot become an admitted observation")
        if (origin["package_root"] not in item["allowed_package_roots"]
                or origin["module_origin"] != origin["package_root"] + "/__init__.py"
                or origin["hash_rule"] != item["hash_rule"]
                or origin["content_digest"] != item["content_digest"]):
            raise ValueError("module observation differs from expected root/content evidence")
    return expected, report


def make_observation(
    binding: PythonModuleObservationBinding,
    expected: ExpectedPythonModules,
    report: PythonModuleReport,
) -> PythonModuleObservationRecord:
    body = {"schema_version": RECORD_VERSION, "binding": binding,
            "expected": expected, "report": report}
    return validate_observation({**body, "observation_id": "sha256:" + digest(body)})


def validate_observation(value: object) -> PythonModuleObservationRecord:
    record = cast(PythonModuleObservationRecord, validated_snapshot(
        value, _VALIDATOR, "PythonModuleObservationRecord"))
    # The record schema already references both complete nested schema authorities.
    expected, report = _normalized_evidence(record["expected"], record["report"])
    if expected != record["expected"] or report != record["report"]:
        raise ValueError("module observation evidence is not normalized")
    body = {key: value for key, value in record.items() if key != "observation_id"}
    if record["observation_id"] != "sha256:" + digest(body):
        raise ValueError("module observation evidence digest differs")
    return record


def stable_module_identity(record: PythonModuleObservationRecord) -> str:
    """Physical origins remain in evidence; admitted expectation/content stays stable."""
    return record["report"]["expected_binding_digest"]
