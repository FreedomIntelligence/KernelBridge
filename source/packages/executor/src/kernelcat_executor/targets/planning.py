"""Deterministic bounded matching; no device discovery, reservation or execution.

Adapters supply qualified observations. A returned plan is a proposal: dispatch must
recheck live environment, permissions, budget and leases at the existing authority.
The bounded inventory scan costs O(devices * adapters * required_capabilities).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TypeVar, cast

from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.models import canonical
from kernelcat_executor.contracts.task_contract import load_task_contract
from kernelcat_executor.generated.targets import (
    SCHEMA,
    AdapterQualification,
    CompatibilityDecision,
    DeviceRef,
    ExecutionPlan,
    TargetSpec,
)

POLICY_VERSION = "kernelcat.target-match/v1"
T = TypeVar("T")


def _validated(value: object, name: str) -> object:
    validator = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/" + name})
    return validated_snapshot(value, validator, name)


def _decision(status: str, reasons: list[str], adapter: AdapterQualification) -> CompatibilityDecision:
    return cast(CompatibilityDecision, {
        "status": status, "reasons": reasons,
        "evidence_refs": adapter["evidence_refs"], "policy_version": POLICY_VERSION,
    })


def _compatibility(target: TargetSpec, device: DeviceRef,
                   adapter: AdapterQualification) -> CompatibilityDecision:
    rejected: list[str] = []
    unknown: list[str] = []
    if device["vendor"] != target["vendor"] or adapter["vendor"] != target["vendor"]:
        rejected.append("vendor mismatch")
    if (device["architecture"] not in target["architectures"]
            or device["architecture"] not in adapter["architectures"]):
        rejected.append("architecture mismatch")
    if target["program_form"] not in adapter["program_forms"]:
        rejected.append("program form unsupported")
    if target["protocol_id"] != adapter["protocol_id"]:
        rejected.append("evaluation protocol mismatch")
    for capability in target["required_capabilities"]:
        status = adapter["capabilities"].get(capability, "unknown")
        if status == "unsupported":
            rejected.append("capability unsupported: " + capability)
        elif status == "unknown":
            unknown.append("capability unknown: " + capability)
    for name, expected in target["requirements"].items():
        if name not in adapter["requirements"]:
            unknown.append("requirement not qualified: " + name)
        elif adapter["requirements"][name] != expected:
            rejected.append("requirement mismatch: " + name)
    if device["environment"]["digest"] not in adapter["environment_digests"]:
        unknown.append("environment not qualified by adapter")
    if adapter["qualification"] == "unsupported":
        rejected.append("adapter qualification rejected")
    elif adapter["qualification"] == "unknown" or not adapter["evidence_refs"]:
        unknown.append("adapter qualification evidence unavailable")
    return _decision("unsupported" if rejected else "unknown" if unknown else "supported",
                     rejected + unknown, adapter)


def _readiness(target: TargetSpec, device: DeviceRef) -> tuple[str, ...]:
    reasons = []
    if device["health"] != "healthy":
        reasons.append("device health: " + device["health"])
    if device["availability"] != "available":
        reasons.append("device availability: " + device["availability"])
    memory = device["available_memory_bytes"]
    if memory == "unknown":
        reasons.append("available memory unknown")
    elif memory < target["memory_bytes"]:
        reasons.append("insufficient available memory")
    return tuple(reasons)


@dataclass(frozen=True)
class PlanCandidate:
    """Immutable canonical proposal; reading its wire value returns a detached copy."""

    payload: bytes
    digest: str

    def as_wire(self) -> ExecutionPlan:
        return cast(ExecutionPlan, json.loads(self.payload))


@dataclass(frozen=True)
class PlanResolution:
    plans: tuple[PlanCandidate, ...]
    qualification_required: tuple[str, ...]
    rejected: tuple[str, ...]
    waiting: tuple[str, ...]


def _unique(values: list[T], key: str, label: str) -> list[T]:
    identities = [cast(dict[str, object], value)[key] for value in values]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate " + label + " identity")
    return values


def _candidate(task_digest: str, program_digest: str, target: TargetSpec,
               device: DeviceRef, adapter: AdapterQualification, revision: int) -> PlanCandidate:
    wire = _validated({
        "schema_version": "kernelcat.execution-plan/v1",
        "task_contract_digest": task_digest, "program_digest": program_digest,
        "target": target, "device": device, "adapter": adapter,
        "inventory_revision": revision,
    }, "ExecutionPlan")
    payload = canonical(wire).encode("utf-8")
    return PlanCandidate(payload, "sha256:" + hashlib.sha256(payload).hexdigest())


def resolve_plans(task_source: bytes, program_digest: str, target_spec: object,
                  inventory: list[object], adapters: list[object], *,
                  inventory_revision: int) -> PlanResolution:
    """Resolve at most 128 devices/adapters; missing support remains explicit unknown.

    The original TaskContract bytes remain its identity. This version handles one
    contract target and does not aggregate performance across different devices.
    """
    if len(inventory) > 128 or len(adapters) > 128:
        raise ValueError("target resolution inventory exceeds 128 entries")
    _validated(program_digest, "Digest")
    _validated(inventory_revision, "Revision")
    task = load_task_contract(task_source)
    target = cast(TargetSpec, _validated(target_spec, "TargetSpec"))
    contract_target = task["contract"]["target"]
    if (target["vendor"] != contract_target["vendor"]
            or target["architectures"] != [contract_target["architecture"]]):
        raise ValueError("target specification differs from frozen TaskContract target")
    if target["protocol_id"] != task["contract"]["measurement_protocol"]["rule_id"]:
        raise ValueError("evaluation protocol differs from frozen TaskContract")
    if target["requirements"] != contract_target["requirements"]:
        raise ValueError("requirements differ from frozen TaskContract")
    devices = _unique([cast(DeviceRef, _validated(item, "DeviceRef"))
                       for item in inventory], "id", "device")
    qualified = [cast(AdapterQualification, _validated(item, "AdapterQualification"))
                 for item in adapters]
    return _resolve(task["source_digest"], program_digest, target, devices,
                    qualified, inventory_revision)


def _resolve(task_digest: str, program_digest: str, target: TargetSpec,
             devices: list[DeviceRef], adapters: list[AdapterQualification],
             revision: int) -> PlanResolution:
    plans: list[PlanCandidate] = []
    unknown, rejected, waiting = [], [], []
    for device in devices:
        for adapter in adapters:
            identity = device["id"] + "/" + adapter["compiler_id"] + "/" + adapter["runtime_id"]
            decision = _compatibility(target, device, adapter)
            reasons = identity + ": " + "; ".join(decision["reasons"])
            if decision["status"] == "unsupported":
                rejected.append(reasons)
            elif decision["status"] == "unknown":
                unknown.append(reasons)
            elif readiness := _readiness(target, device):
                waiting.append(identity + ": " + "; ".join(readiness))
            else:
                plans.append(_candidate(task_digest, program_digest, target, device, adapter, revision))
    if not adapters:
        unknown.append("no registered adapter qualification")
    if not devices:
        unknown.append("no observed device inventory")
    unique_plans = {plan.digest: plan for plan in plans}
    return PlanResolution(tuple(unique_plans[key] for key in sorted(unique_plans)),
                          tuple(sorted(set(unknown))), tuple(sorted(set(rejected))),
                          tuple(sorted(set(waiting))))
