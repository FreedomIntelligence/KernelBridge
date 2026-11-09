#!/usr/bin/env python3
"""Stable diagnosis-first Agent entrypoint for completed profiler runs."""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from profiler_diagnosis import compact_diagnosis

SCHEMA_VERSION = "ascend-profiler-agent-diagnosis/v1"


def _estimate_tokens(payload: Mapping[str, Any]) -> int:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    return max(1, cjk + (len(text) - cjk + 3) // 4)


def _decision(pair: Mapping[str, Any] | None) -> dict[str, Any]:
    pair = pair or {}
    event = pair.get("event") if isinstance(pair.get("event"), Mapping) else {}
    passed = event.get("passed") is True
    return {
        "promotion_eligible": passed and pair.get("knowledge_status") == "proven_policy",
        "recommendation": (
            "eligible_for_parent_promotion" if passed and pair.get("knowledge_status") == "proven_policy"
            else "do_not_promote; execute next evidence action"
        ),
        "reward_authority": "profiler-off NPU Event",
        "pmu_role": "mechanism_only",
    }


def build_diagnosis_first_projection(
    diagnosis: Mapping[str, Any],
    *,
    token_budget: int,
    legacy_entrypoint: str = "agent_feedback.json",
    audit_entrypoint: str = "unified_feedback.json",
    diagnosis_entrypoint: str = "diagnosis.json",
) -> dict[str, Any]:
    if token_budget < 128:
        raise ValueError("token_budget must be at least 128")
    variants = (
        dict(max_pairs=2, max_bottlenecks=2, max_suspects=3, max_policies=3, max_checks=4),
        dict(max_pairs=1, max_bottlenecks=2, max_suspects=2, max_policies=2, max_checks=3),
        dict(max_pairs=1, max_bottlenecks=1, max_suspects=1, max_policies=2, max_checks=2),
    )
    last: dict[str, Any] | None = None
    for limits in variants:
        compact = compact_diagnosis(diagnosis, **limits)
        pair = (compact.get("pairs") or [None])[0]
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": compact.get("status"),
            "decision": _decision(pair if isinstance(pair, Mapping) else None),
            "authority": compact.get("authority"),
            "summary": compact.get("summary"),
            "diagnosis": compact,
            "entrypoints": {
                "legacy_agent": legacy_entrypoint,
                "audit": audit_entrypoint,
                "diagnosis": diagnosis_entrypoint,
            },
            "extraction": {
                "token_budget": token_budget,
                "tokens": 0,
                "budget_satisfied": False,
                "variant": limits,
            },
        }
        payload["extraction"]["tokens"] = _estimate_tokens(payload)
        if payload["extraction"]["tokens"] <= token_budget:
            payload["extraction"]["budget_satisfied"] = True
            payload["extraction"]["tokens"] = _estimate_tokens(payload)
            return payload
        last = payload
    assert last is not None
    minimal = copy.deepcopy(last)
    diagnosis_payload = minimal.get("diagnosis") or {}
    diagnosis_payload["blind_spots"] = []
    pairs = diagnosis_payload.get("pairs") or []
    if pairs:
        pairs[0]["proof_blockers"] = list(pairs[0].get("proof_blockers") or [])[:4]
    minimal["extraction"]["variant"] = "minimal"
    minimal["extraction"]["tokens"] = _estimate_tokens(minimal)
    if minimal["extraction"]["tokens"] > token_budget:
        raise ValueError(
            f"diagnosis-first projection exceeded token budget: {minimal['extraction']['tokens']}/{token_budget}"
        )
    minimal["extraction"]["budget_satisfied"] = True
    minimal["extraction"]["tokens"] = _estimate_tokens(minimal)
    return minimal


def serialize_agent_diagnosis(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
