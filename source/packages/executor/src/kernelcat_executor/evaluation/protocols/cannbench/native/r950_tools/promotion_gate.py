#!/usr/bin/env python3
"""Hash-bound pre-official gate for 910C campaign candidates.

The gate lives beside the operator source directory:

    arms/<arm>/GATE.json
    arms/<arm>/<op_snake>/...

It is deliberately dependency-free so push910 can fail closed before touching
an account, a credit, or a remote submission slot.
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def gate_path_for(srcdir: str | pathlib.Path) -> pathlib.Path:
    src = pathlib.Path(srcdir).resolve()
    own = src / "GATE.json"
    return own if own.is_file() else src.parent / "GATE.json"


def validate_gate(op: str, srcdir: str | pathlib.Path, source_sha256: str,
                  *, required: bool = False) -> tuple[pathlib.Path | None, dict[str, Any] | None, list[str]]:
    """Return ``(path, payload, errors)`` without mutating any state.

    When ``required`` is false and no gate exists, the result has no errors for
    backward-compatible rerolls.  If a gate exists, it is always validated;
    a stale or malformed gate is never silently ignored.
    """
    path = gate_path_for(srcdir)
    if not path.is_file():
        errors = [f"missing gate: {path}"] if required else []
        return None, None, errors
    try:
        gate = json.loads(path.read_text())
    except Exception as exc:
        return path, None, [f"unreadable gate: {exc}"]

    errors: list[str] = []
    if _norm(gate.get("operator", "")) != _norm(op):
        errors.append(f"operator mismatch: gate={gate.get('operator')!r} submit={op!r}")
    if gate.get("push_source_sha256") != source_sha256:
        errors.append("source hash mismatch: GATE.json is stale for this operator tree")
    if gate.get("soc") != "910c":
        errors.append(f"soc must be 910c, got {gate.get('soc')!r}")
    if gate.get("compute_unit") != "ascend910_93":
        errors.append(f"compute_unit must be ascend910_93, got {gate.get('compute_unit')!r}")
    if gate.get("device_isa") != "DAV2201":
        errors.append(f"device_isa must be DAV2201, got {gate.get('device_isa')!r}")
    for field in ("parent_sha256", "candidate_sha256", "casepack_sha256"):
        if not isinstance(gate.get(field), str) or not gate[field].strip():
            errors.append(f"missing {field}")
    for field in ("host_proof", "compile", "representative_exact",
                  "fallback_exact", "same_card_full20"):
        check = gate.get(field)
        if not isinstance(check, dict) or check.get("status") != "pass":
            errors.append(f"{field}.status must be pass")
    delta = gate.get("current_parent_delta")
    if not isinstance(delta, (int, float)) or isinstance(delta, bool) or delta <= 0:
        errors.append(f"current_parent_delta must be > 0, got {delta!r}")
    if gate.get("eligible_official") is not True:
        errors.append("eligible_official must be true")
    if not isinstance(gate.get("hit_symbols"), list) or not gate["hit_symbols"]:
        errors.append("hit_symbols must be a non-empty list")
    return path, gate, errors

