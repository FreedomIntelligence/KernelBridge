#!/usr/bin/env python3
"""Deterministic Profiler-to-code diagnosis for completed Ascend 910C A/B runs.

Profiler-off NPU Event is the latency authority; PMU is mechanism evidence;
verified route-hit and source-change evidence are required for L2/L3.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from profiler_diagnosis_rules import (
    AUTHORITY_CONTRACT,
    COMPACT_DIAGNOSIS_SCHEMA_VERSION,
    DIAGNOSIS_SCHEMA_VERSION,
    _EVENT_GATE,
    _GLOBAL_BLIND_SPOTS,
    _RuleResult,
    _canonical,
    _case_index,
    _compact_number,
    _mapping,
    _number,
    _rule_results,
    _sequence,
)


def _timing_pairs(feedback: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    pairs: dict[str, dict[str, Any]] = {}
    for analysis in _sequence(_mapping(feedback.get("timing")).get("analyses")):
        if not isinstance(analysis, Mapping):
            continue
        capture = _mapping(analysis.get("capture_metadata"))
        base_arm = str(analysis.get("base") or _mapping(feedback.get("query")).get("base_arm") or "base")
        for candidate_arm, arm_result in sorted(_mapping(analysis.get("diff")).items()):
            for row in _sequence(_mapping(arm_result).get("rows")):
                if not isinstance(row, Mapping):
                    continue
                pair_id = str(row.get("pair_id") or "")
                case_id = _case_index(row.get("idx"))
                if not pair_id or case_id is None:
                    continue
                pairs[pair_id] = {
                    "pair_id": pair_id,
                    "run_id": str(analysis.get("run_id") or ""),
                    "operator": str(analysis.get("op") or ""),
                    "case_id": case_id,
                    "base_arm": base_arm,
                    "candidate_arm": str(candidate_arm),
                    "base_us": _compact_number(row.get("base_us"), 4),
                    "candidate_us": _compact_number(row.get("mt_us"), 4),
                    "speedup": _compact_number(row.get("x"), 5),
                    "lcb95": _compact_number(row.get("speedup_95pct_lcb"), 5),
                    "stable": row.get("stable") is True,
                    "accepted": row.get("accepted") is True,
                    "protocol": {
                        key: capture.get(key)
                        for key in (
                            "device",
                            "timer",
                            "warmup",
                            "inner_repeat",
                            "timing_scope",
                            "cache_clean_each_sample",
                        )
                        if capture.get(key) is not None
                    },
                    "artifact_refs": sorted(set(analysis.get("artifact_refs") or [])),
                    "provenance": dict(_mapping(analysis.get("provenance"))),
                }
    return pairs


def _correctness_gate(feedback: Mapping[str, Any]) -> dict[str, Any]:
    correctness = _mapping(feedback.get("correctness"))
    complete = correctness.get("complete") is True
    failed = int(_number(correctness.get("failed")) or 0)
    unknown = int(_number(correctness.get("unknown")) or 0)
    passed = complete and failed == 0 and unknown == 0 and correctness.get("all_passed") is True
    return {
        "passed": passed,
        "complete": complete,
        "cases": int(_number(correctness.get("cases")) or 0),
        "failed": failed,
        "unknown": unknown,
    }


def _run_contexts(feedback: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for context in _sequence(feedback.get("run_contexts")):
        if isinstance(context, Mapping):
            result.setdefault(str(context.get("run_id") or ""), []).append(context)
    return result


def _valid_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "")))


def _provenance_gate(contexts: Sequence[Mapping[str, Any]], candidate_arm: str) -> dict[str, Any]:
    hashes: dict[str, Any] = {}
    refs: set[str] = set()
    for context in contexts:
        source_hashes = _mapping(context.get("source_tree_sha256"))
        if source_hashes:
            hashes.update(source_hashes)
        refs.update(str(ref) for ref in context.get("artifact_refs") or [])
    candidate_keys = (candidate_arm, "mt", "candidate")
    candidate_hash = next((hashes[key] for key in candidate_keys if key in hashes), None)
    verified = _valid_sha256(hashes.get("base")) and _valid_sha256(candidate_hash)
    return {
        "status": "verified" if verified else "missing_or_invalid",
        "verified": verified,
        "source_tree_sha256": hashes,
        "artifact_refs": sorted(refs),
    }


def _external_for_pair(
    evidence: Mapping[str, Any] | None,
    *,
    pair_id: str,
    run_id: str,
    case_id: int,
    candidate_arm: str,
) -> Mapping[str, Any]:
    if not isinstance(evidence, Mapping):
        return {}
    direct_fields = {
        "route_hit",
        "observed_route",
        "expected_route",
        "tiling_key",
        "route_key",
        "capability_predicate",
        "build_identity",
        "policy_id",
        "action_cell",
        "changed_symbols",
        "changed_regions",
        "verified",
    }
    if direct_fields & set(evidence):
        return evidence
    keys = (
        pair_id,
        run_id,
        f"{run_id}:{case_id}:{candidate_arm}",
        f"case{case_id}:{candidate_arm}",
        "default",
    )
    for key in keys:
        value = evidence.get(key)
        if isinstance(value, Mapping):
            return value
    for item in _sequence(evidence.get("pairs")):
        if not isinstance(item, Mapping):
            continue
        if item.get("pair_id") == pair_id or (
            str(item.get("run_id") or "") == run_id
            and _case_index(item.get("case_id")) == case_id
            and str(item.get("candidate_arm") or candidate_arm) == candidate_arm
        ):
            return item
    return {}


def _context_evidence(
    contexts: Sequence[Mapping[str, Any]], keys: Sequence[str]
) -> Mapping[str, Any]:
    for context in contexts:
        for key in keys:
            value = context.get(key)
            if isinstance(value, Mapping):
                return value
        execution = _mapping(context.get("execution"))
        for key in keys:
            value = execution.get(key)
            if isinstance(value, Mapping):
                return value
    return {}


def _merge_evidence(primary: Mapping[str, Any], fallback: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(fallback)
    result.update(primary)
    return result


def _route_gate(route: Mapping[str, Any], *, action_cell: str) -> dict[str, Any]:
    expected = str(route.get("expected_route") or "").strip()
    observed = str(route.get("observed_route") or route.get("route") or "").strip()
    route_action = str(route.get("action_cell") or "").strip()
    refs = sorted(set(str(ref) for ref in route.get("artifact_refs") or route.get("refs") or []))
    build_identity = route.get("build_identity") or route.get("build_id") or route.get("build_sha")
    tiling_key = route.get("tiling_key") if "tiling_key" in route else route.get("route_key")
    capability = route.get("capability_predicate")
    missing = []
    if route.get("route_hit") is not True:
        missing.append("route_hit=true")
    if not observed:
        missing.append("observed_route")
    if tiling_key in (None, ""):
        missing.append("tiling_key_or_route_key")
    if capability in (None, ""):
        missing.append("capability_predicate")
    if not route_action:
        missing.append("action_cell")
    if build_identity in (None, "", {}):
        missing.append("build_identity")
    if not refs:
        missing.append("artifact_refs")
    mismatch = bool(expected and observed and _canonical(expected) != _canonical(observed))
    action_mismatch = bool(route_action and action_cell and route_action != action_cell)
    if mismatch or action_mismatch:
        status = "mismatch"
    elif not route:
        status = "missing"
    elif missing:
        status = "asserted_unverified"
    else:
        status = "verified"
    return {
        "status": status,
        "verified": status == "verified",
        "route_hit": route.get("route_hit") is True,
        "expected_route": expected or None,
        "observed_route": observed or None,
        "tiling_key": tiling_key,
        "capability_predicate": capability,
        "action_cell": route_action or None,
        "build_identity": build_identity,
        "artifact_refs": refs,
        "missing_fields": missing,
        "mismatch": mismatch or action_mismatch,
    }


def _code_gate(
    code: Mapping[str, Any], *, action_cell: str, matched_rule_ids: set[str]
) -> dict[str, Any]:
    policy_id = str(code.get("policy_id") or code.get("action_cell") or "").strip()
    symbols = [str(item) for item in code.get("changed_symbols") or [] if str(item).strip()]
    regions = [str(item) for item in code.get("changed_regions") or [] if str(item).strip()]
    refs = sorted(set(str(ref) for ref in code.get("artifact_refs") or code.get("refs") or []))
    source_hashes = _mapping(code.get("source_tree_sha256") or code.get("source_identity"))
    base_hash = source_hashes.get("base") or code.get("source_sha_before")
    candidate_hash = (
        source_hashes.get("candidate")
        or source_hashes.get("mt")
        or code.get("source_sha_after")
    )
    declared_rules = {
        str(item) for item in code.get("bottleneck_ids") or code.get("mechanism_ids") or []
    }
    missing = []
    if code.get("verified") is not True and str(code.get("status") or "") != "verified":
        missing.append("verified=true")
    if not policy_id:
        missing.append("policy_id_or_action_cell")
    if not symbols and not regions:
        missing.append("changed_symbols_or_regions")
    if not _valid_sha256(base_hash) or not _valid_sha256(candidate_hash):
        missing.append("source_sha_before_after")
    if not refs:
        missing.append("artifact_refs")
    policy_mismatch = bool(policy_id and action_cell and policy_id != action_cell)
    rule_mismatch = bool(declared_rules and matched_rule_ids and not (declared_rules & matched_rule_ids))
    if policy_mismatch or rule_mismatch:
        status = "mismatch"
    elif not code:
        status = "missing"
    elif missing:
        status = "asserted_unverified"
    else:
        status = "verified"
    return {
        "status": status,
        "verified": status == "verified",
        "policy_id": policy_id or None,
        "changed_symbols": symbols,
        "changed_regions": regions,
        "source_tree_sha256": {"base": base_hash, "candidate": candidate_hash},
        "declared_bottleneck_ids": sorted(declared_rules),
        "artifact_refs": refs,
        "missing_fields": missing,
        "mismatch": policy_mismatch or rule_mismatch,
    }


def _event_gate(event: Mapping[str, Any]) -> dict[str, Any]:
    speedup = _number(event.get("speedup"))
    lcb = _number(event.get("lcb95"))
    stable = event.get("stable") is True
    accepted = event.get("accepted") is True
    passed = stable and accepted and speedup is not None and speedup > 1.0 and lcb is not None and lcb > 1.0
    return {
        "passed": passed,
        "authority": "profiler-off NPU Event",
        "base_us": event.get("base_us"),
        "candidate_us": event.get("candidate_us"),
        "speedup": event.get("speedup"),
        "lcb95": event.get("lcb95"),
        "stable": stable,
        "accepted": accepted,
    }


def _mechanism_gate(comparison: Mapping[str, Any]) -> dict[str, Any]:
    expected = _mapping(comparison.get("expected_direction"))
    verdict = str(expected.get("verdict") or "missing")
    checks = []
    for item in _sequence(expected.get("checks")):
        if not isinstance(item, Mapping):
            continue
        checks.append(
            {
                "field": item.get("field"),
                "expected": item.get("expected"),
                "observed": item.get("observed"),
                "matches": item.get("matches") is True,
                "relative_delta": _compact_number(item.get("relative_delta"), 4),
            }
        )
    return {
        "status": verdict,
        "supported": verdict == "supported",
        "checks": checks,
        "pmu_is_reward": False,
    }


def _level_label(level: int) -> str:
    return {
        0: "L0_observation",
        1: "L1_hardware_mechanism",
        2: "L2_code_attribution",
        3: "L3_causal_proof",
    }[max(0, min(level, 3))]


def _knowledge_status(level: int, correctness: Mapping[str, Any]) -> str:
    if correctness.get("passed") is False and (
        int(correctness.get("failed") or 0) > 0 or int(correctness.get("unknown") or 0) > 0
    ):
        return "rejected"
    return {
        0: "observation",
        1: "hardware_hypothesis",
        2: "code_attributed",
        3: "proven_policy",
    }[max(0, min(level, 3))]


def _proof_blockers(
    *,
    level: int,
    event: Mapping[str, Any],
    correctness: Mapping[str, Any],
    provenance: Mapping[str, Any],
    route: Mapping[str, Any],
    code: Mapping[str, Any],
    mechanism: Mapping[str, Any],
    bottlenecks: Sequence[_RuleResult],
) -> list[str]:
    blockers = []
    if not bottlenecks:
        blockers.append("no_bottleneck_signature_matched")
    if not route.get("verified"):
        blockers.append(
            "route_evidence_mismatch" if route.get("status") == "mismatch" else "route_evidence_missing_or_unverified"
        )
    if not code.get("verified"):
        blockers.append(
            "code_attribution_mismatch" if code.get("status") == "mismatch" else "code_attribution_missing_or_unverified"
        )
    if not event.get("passed"):
        blockers.append("event_gate_not_passed")
    if not correctness.get("passed"):
        blockers.append("correctness_gate_not_passed")
    if not provenance.get("verified"):
        blockers.append("source_provenance_not_verified")
    if not mechanism.get("supported"):
        blockers.append("expected_pmu_direction_not_supported")
    if level == 3:
        return []
    return blockers


def _rule_payload(result: _RuleResult) -> dict[str, Any]:
    return {
        "bottleneck_id": result.rule_id,
        "title": result.title,
        "mapping_confidence": result.confidence,
        "score": _compact_number(result.score, 4),
        "signature": {
            "matched_fields": list(result.matched),
            "derived": dict(result.derived),
            "interpretation": result.mechanism,
        },
        "suspect_code": list(result.suspect_code),
        "candidate_policies": [dict(policy) for policy in result.candidate_policies],
        "expected_pmu_delta": [dict(item) for item in result.expected_pmu_delta],
        "event_gate": dict(_EVENT_GATE),
        "disambiguation_checks": [dict(item) for item in result.disambiguation_checks],
        "blind_spots": list(result.blind_spots),
    }


def build_profiler_diagnosis(
    feedback: Mapping[str, Any],
    *,
    route_evidence: Mapping[str, Any] | None = None,
    code_attribution: Mapping[str, Any] | None = None,
    max_bottlenecks: int = 6,
) -> dict[str, Any]:
    """Build a fail-closed Profiler→hardware→code→proof diagnosis.

    ``feedback`` is the full output from ``profiler_feedback_sdk``.  Optional
    ``route_evidence`` and ``code_attribution`` can supplement run metadata;
    they never override a detected mismatch and are validated before L2/L3.
    """
    if max_bottlenecks < 1:
        raise ValueError("max_bottlenecks must be positive")
    action_cell = str(_mapping(feedback.get("query")).get("action_cell") or "")
    timing = _timing_pairs(feedback)
    contexts_by_run = _run_contexts(feedback)
    correctness = _correctness_gate(feedback)
    comparisons = _sequence(_mapping(feedback.get("pmu")).get("comparisons"))
    comparisons_by_pair = {
        str(comparison.get("selected_timing_pair_ref") or ""): comparison
        for comparison in comparisons
        if isinstance(comparison, Mapping)
        and str(comparison.get("selected_timing_pair_ref") or "")
    }
    diagnoses: list[dict[str, Any]] = []

    # Event timing remains useful even when PMU collection failed.  Such a pair
    # is emitted as L0 (or L1 when launch/execution evidence matches) instead of
    # disappearing merely because mechanism evidence is absent.
    for pair_id, event in sorted(timing.items()):
        comparison = comparisons_by_pair.get(pair_id, {})
        run_id = str(comparison.get("run_id") or event.get("run_id") or "")
        case_id = int(_case_index(comparison.get("case_id")) or event.get("case_id") or -1)
        candidate_arm = str(comparison.get("candidate_arm") or event.get("candidate_arm") or "")
        run_contexts = contexts_by_run.get(run_id, [])
        rules = _rule_results(feedback, comparison, event)[:max_bottlenecks]
        rule_ids = {item.rule_id for item in rules}

        route_from_context = _context_evidence(run_contexts, ("route_identity", "route_evidence"))
        route_external = _external_for_pair(
            route_evidence,
            pair_id=pair_id,
            run_id=run_id,
            case_id=case_id,
            candidate_arm=candidate_arm,
        )
        route = _route_gate(
            _merge_evidence(route_external, route_from_context),
            action_cell=action_cell,
        )

        code_from_context = _context_evidence(run_contexts, ("code_attribution", "patch_attribution"))
        code_external = _external_for_pair(
            code_attribution,
            pair_id=pair_id,
            run_id=run_id,
            case_id=case_id,
            candidate_arm=candidate_arm,
        )
        code = _code_gate(
            _merge_evidence(code_external, code_from_context),
            action_cell=action_cell,
            matched_rule_ids=rule_ids,
        )

        event_decision = _event_gate(event)
        provenance = _provenance_gate(run_contexts, candidate_arm)
        mechanism = _mechanism_gate(comparison)
        level = 0
        if rules:
            level = 1
        if level >= 1 and route.get("verified") and code.get("verified"):
            level = 2
        if (
            level >= 2
            and event_decision.get("passed")
            and correctness.get("passed")
            and provenance.get("verified")
            and mechanism.get("supported")
        ):
            level = 3
        blockers = _proof_blockers(
            level=level,
            event=event_decision,
            correctness=correctness,
            provenance=provenance,
            route=route,
            code=code,
            mechanism=mechanism,
            bottlenecks=rules,
        )
        diagnosis_id = f"{run_id}:{event.get('operator')}:{case_id}:{candidate_arm}:{pair_id}"
        diagnoses.append(
            {
                "diagnosis_id": diagnosis_id,
                "identity": {
                    "run_id": run_id,
                    "operator": event.get("operator"),
                    "case_id": case_id,
                    "base_arm": event.get("base_arm"),
                    "candidate_arm": candidate_arm,
                    "timing_pair_ref": pair_id,
                    "action_cell": action_cell,
                },
                "attribution": {
                    "level": f"L{level}",
                    "label": _level_label(level),
                    "knowledge_status": _knowledge_status(level, correctness),
                    "proof_blockers": blockers,
                },
                "gates": {
                    "event": event_decision,
                    "correctness": dict(correctness),
                    "provenance": provenance,
                    "route": route,
                    "code_attribution": code,
                    "mechanism": mechanism,
                },
                "observations": {
                    "event": event_decision,
                    "pmu_artifact_refs": sorted(
                        set(comparison.get("base_artifact_refs") or [])
                        | set(comparison.get("candidate_artifact_refs") or [])
                    ),
                    "profiler_duration_is_reward": False,
                },
                "bottlenecks": [_rule_payload(item) for item in rules],
            }
        )

    highest = max((int(item["attribution"]["level"][1:]) for item in diagnoses), default=0)
    proven = sum(item["attribution"]["knowledge_status"] == "proven_policy" for item in diagnoses)
    rejected = sum(item["attribution"]["knowledge_status"] == "rejected" for item in diagnoses)
    if not diagnoses:
        status = "rejected"
    elif proven:
        status = "ok"
    else:
        status = "partial"
    return {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "source_schema_version": feedback.get("schema_version"),
        "status": status,
        "authority_contract": dict(AUTHORITY_CONTRACT),
        "query": {
            "action_cell": action_cell,
            "base_arm": _mapping(feedback.get("query")).get("base_arm"),
            "timing_stat": _mapping(feedback.get("query")).get("timing_stat"),
        },
        "summary": {
            "diagnoses": len(diagnoses),
            "highest_attribution_level": f"L{highest}",
            "proven_policies": proven,
            "rejected_pairs": rejected,
            "route_verified_pairs": sum(item["gates"]["route"]["verified"] for item in diagnoses),
            "code_attributed_pairs": sum(item["gates"]["code_attribution"]["verified"] for item in diagnoses),
        },
        "diagnoses": diagnoses,
        "blind_spots": list(_GLOBAL_BLIND_SPOTS),
    }


def compact_diagnosis(
    diagnosis: Mapping[str, Any],
    *,
    max_pairs: int = 3,
    max_bottlenecks: int = 2,
    max_suspects: int = 2,
    max_policies: int = 3,
    max_checks: int = 3,
) -> dict[str, Any]:
    """Return an Agent-oriented projection without raw counter payloads."""
    if min(max_pairs, max_bottlenecks, max_suspects, max_policies, max_checks) < 1:
        raise ValueError("compact limits must be positive")
    pairs = []
    for item in list(_sequence(diagnosis.get("diagnoses")))[:max_pairs]:
        if not isinstance(item, Mapping):
            continue
        identity = _mapping(item.get("identity"))
        attribution = _mapping(item.get("attribution"))
        gates = _mapping(item.get("gates"))
        event = _mapping(gates.get("event"))
        compact_bottlenecks = []
        for bottleneck in list(_sequence(item.get("bottlenecks")))[:max_bottlenecks]:
            if not isinstance(bottleneck, Mapping):
                continue
            signature = _mapping(bottleneck.get("signature"))
            policies = []
            for policy in list(_sequence(bottleneck.get("candidate_policies")))[:max_policies]:
                if isinstance(policy, Mapping):
                    policies.append(
                        {
                            "id": policy.get("id"),
                            "automatic": False,
                            "preconditions": list(_sequence(policy.get("preconditions")))[:2],
                        }
                    )
            checks = []
            for check in list(_sequence(bottleneck.get("disambiguation_checks")))[:max_checks]:
                if isinstance(check, Mapping):
                    checks.append({"id": check.get("id"), "question": check.get("question")})
            matched = []
            for row in list(_sequence(signature.get("matched_fields")))[:3]:
                if isinstance(row, Mapping):
                    matched.append(
                        {
                            key: row.get(key)
                            for key in ("field", "base", "candidate", "relative_delta")
                            if row.get(key) is not None
                        }
                    )
            compact_bottlenecks.append(
                {
                    "id": bottleneck.get("bottleneck_id"),
                    "confidence": bottleneck.get("mapping_confidence"),
                    "why": signature.get("interpretation"),
                    "matched": matched,
                    "suspect_code": list(_sequence(bottleneck.get("suspect_code")))[:max_suspects],
                    "candidate_policies": policies,
                    "next_checks": checks,
                    "event_gate": "stable profiler-off Event with LCB95 > 1 and unchanged correctness",
                }
            )
        pairs.append(
            {
                "pair_id": identity.get("timing_pair_ref"),
                "run_id": identity.get("run_id"),
                "operator": identity.get("operator"),
                "case_id": identity.get("case_id"),
                "candidate_arm": identity.get("candidate_arm"),
                "level": attribution.get("level"),
                "knowledge_status": attribution.get("knowledge_status"),
                "event": {
                    key: event.get(key)
                    for key in ("base_us", "candidate_us", "speedup", "lcb95", "stable", "accepted", "passed")
                },
                "route": _mapping(gates.get("route")).get("status"),
                "code_attribution": _mapping(gates.get("code_attribution")).get("status"),
                "mechanism": _mapping(gates.get("mechanism")).get("status"),
                "proof_blockers": list(_sequence(attribution.get("proof_blockers")))[:8],
                "bottlenecks": compact_bottlenecks,
            }
        )
    return {
        "schema_version": COMPACT_DIAGNOSIS_SCHEMA_VERSION,
        "status": diagnosis.get("status"),
        "authority": {
            "reward": "profiler-off Event",
            "pmu": "mechanism_only",
            "route_required_for_code_attribution": True,
        },
        "summary": dict(_mapping(diagnosis.get("summary"))),
        "pairs": pairs,
        "blind_spots": list(_sequence(diagnosis.get("blind_spots")))[:5],
    }


def serialize_diagnosis(payload: Mapping[str, Any], *, pretty: bool = True) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def query_profiler_diagnosis(
    artifact_roots: Iterable[str | Path],
    *,
    action_cell: str,
    base_arm: str = "base",
    stat: str = "p50",
    expected_directions: Mapping[str, str] | None = None,
    route_evidence: Mapping[str, Any] | None = None,
    code_attribution: Mapping[str, Any] | None = None,
    max_bottlenecks: int = 6,
) -> dict[str, Any]:
    """Query the full feedback SDK exactly once, then diagnose it locally."""
    from profiler_feedback_sdk import query_profiler_feedback

    feedback = query_profiler_feedback(
        artifact_roots,
        action_cell=action_cell,
        base_arm=base_arm,
        stat=stat,
        expected_directions=expected_directions,
    )
    return build_profiler_diagnosis(
        feedback,
        route_evidence=route_evidence,
        code_attribution=code_attribution,
        max_bottlenecks=max_bottlenecks,
    )


def _load_json(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", action="append", required=True, type=Path)
    parser.add_argument("--action-cell", required=True)
    parser.add_argument("--base-arm", default="base")
    parser.add_argument("--stat", default="p50", choices=("p50", "p95", "p99"))
    parser.add_argument("--route-evidence", type=Path)
    parser.add_argument("--code-attribution", type=Path)
    parser.add_argument("--max-bottlenecks", type=int, default=6)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload = query_profiler_diagnosis(
            args.artifact_root,
            action_cell=args.action_cell,
            base_arm=args.base_arm,
            stat=args.stat,
            route_evidence=_load_json(args.route_evidence),
            code_attribution=_load_json(args.code_attribution),
            max_bottlenecks=args.max_bottlenecks,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.compact:
        payload = compact_diagnosis(payload)
    if args.output.exists():
        print(f"refusing existing output: {args.output}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialize_diagnosis(payload, pretty=not args.compact), encoding="utf-8")
    print(f"wrote {args.output}")
    return {"ok": 0, "partial": 3, "rejected": 4}.get(str(payload.get("status")), 4)


if __name__ == "__main__":
    raise SystemExit(main())
