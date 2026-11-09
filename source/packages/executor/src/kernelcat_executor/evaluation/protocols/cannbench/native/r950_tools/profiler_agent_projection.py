#!/usr/bin/env python3
"""Agent/audit/diagnosis views over the lossless profiler feedback SDK.

The original token-budgeted projection implementation is preserved in
``profiler_agent_projection_core``.  This compatibility façade adds a compact,
fail-closed Profiler→code diagnosis while keeping the existing public API and
schema stable for callers such as ``profiler_run.py``.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import profiler_agent_projection_core as _core
from profiler_diagnosis import (
    build_profiler_diagnosis,
    compact_diagnosis,
    serialize_diagnosis,
)


AGENT_SCHEMA_VERSION = _core.AGENT_SCHEMA_VERSION
AUDIT_SCHEMA_VERSION = _core.AUDIT_SCHEMA_VERSION
EXTRACTION_ALGORITHM = _core.EXTRACTION_ALGORITHM
DEFAULT_TOKEN_BUDGET = _core.DEFAULT_TOKEN_BUDGET
EvidenceAtom = _core.EvidenceAtom
serialize_projection = _core.serialize_projection



def _projection_diagnosis(
    diagnosis: Mapping[str, Any], *, minimal: bool = False
) -> dict[str, Any]:
    compact = compact_diagnosis(
        diagnosis,
        max_pairs=1,
        max_bottlenecks=1,
        max_suspects=1,
        max_policies=2,
        max_checks=2,
    )
    if not minimal:
        return compact
    pairs = []
    for pair in compact.get("pairs") or []:
        bottlenecks = []
        for bottleneck in pair.get("bottlenecks") or []:
            bottlenecks.append(
                {
                    "id": bottleneck.get("id"),
                    "confidence": bottleneck.get("confidence"),
                    "policies": [
                        policy.get("id")
                        for policy in bottleneck.get("candidate_policies") or []
                    ],
                    "checks": [
                        check.get("id") for check in bottleneck.get("next_checks") or []
                    ],
                }
            )
        pairs.append(
            {
                "pair_id": pair.get("pair_id"),
                "level": pair.get("level"),
                "knowledge_status": pair.get("knowledge_status"),
                "event": pair.get("event"),
                "route": pair.get("route"),
                "code_attribution": pair.get("code_attribution"),
                "proof_blockers": pair.get("proof_blockers"),
                "bottlenecks": bottlenecks,
            }
        )
    return {
        "schema_version": compact.get("schema_version"),
        "status": compact.get("status"),
        "authority": compact.get("authority"),
        "summary": compact.get("summary"),
        "pairs": pairs,
    }



def _measure(projection: dict[str, Any], counter: Callable[[str], int]) -> int:
    return _core._stabilize_token_count(projection, counter)



def _attach_diagnosis_under_budget(
    projection: dict[str, Any],
    diagnosis: Mapping[str, Any],
    *,
    token_budget: int,
    token_counter: Callable[[str], int] | None,
) -> dict[str, Any]:
    # Preserve the core fail-closed behavior when mandatory safety evidence
    # already exceeds the budget.  Diagnosis remains available via --view
    # diagnosis and is not allowed to displace correctness/caveats.
    if (projection.get("extraction") or {}).get("budget_satisfied") is False:
        return projection

    counter, _, _ = _core._resolve_counter(token_counter)
    original = copy.deepcopy(projection)

    # Compatibility is stronger than enrichment: the diagnosis may use only
    # genuine spare budget.  It must not reorder or evict evidence selected by
    # the frozen core projection, even when that evidence is optional.
    for variant, minimal in (("standard", False), ("minimal", True)):
        candidate = copy.deepcopy(original)
        candidate["diagnosis"] = _projection_diagnosis(diagnosis, minimal=minimal)
        extraction = candidate.setdefault("extraction", {})
        extraction["diagnosis_included"] = True
        extraction["diagnosis_variant"] = variant
        measured = _measure(candidate, counter)
        if measured <= token_budget:
            extraction["budget_satisfied"] = True
            _measure(candidate, counter)
            return candidate

    # No spare budget: return the exact core projection.  The full diagnosis
    # remains queryable via --view diagnosis without changing legacy ranking.
    return original


def build_agent_projection(
    feedback: Mapping[str, Any],
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    token_counter: Callable[[str], int] | None = None,
    route_evidence: Mapping[str, Any] | None = None,
    code_attribution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the existing Agent view and add the highest-value diagnosis that fits."""
    projection = _core.build_agent_projection(
        feedback,
        token_budget=token_budget,
        token_counter=token_counter,
    )
    diagnosis = build_profiler_diagnosis(
        feedback,
        route_evidence=route_evidence,
        code_attribution=code_attribution,
    )
    return _attach_diagnosis_under_budget(
        projection,
        diagnosis,
        token_budget=token_budget,
        token_counter=token_counter,
    )


def build_audit_projection(
    feedback: Mapping[str, Any],
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    token_counter: Callable[[str], int] | None = None,
    route_evidence: Mapping[str, Any] | None = None,
    code_attribution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return enriched Agent context plus the existing payload-free audit index."""
    agent = build_agent_projection(
        feedback,
        token_budget=token_budget,
        token_counter=token_counter,
        route_evidence=route_evidence,
        code_attribution=code_attribution,
    )
    entries = []
    for entry in (feedback.get("evidence_catalog") or {}).get("entries") or []:
        entries.append(
            {
                "id": entry.get("id"),
                "kind": entry.get("kind"),
                "artifact_refs": list(entry.get("artifact_refs") or []),
                "canonical_sha256": entry.get("canonical_sha256"),
                "role": entry.get("role"),
                "validation": entry.get("validation"),
            }
        )
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "agent": agent,
        "audit": {
            "artifacts": feedback.get("artifacts") or {},
            "run_contexts": feedback.get("run_contexts") or [],
            "evidence_index": entries,
            "payloads_embedded": False,
        },
    }


def query_profiler_feedback_view(
    artifact_roots: Iterable[str | Path],
    *,
    action_cell: str,
    base_arm: str = "base",
    stat: str = "p50",
    expected_directions: Mapping[str, str] | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    view: str = "agent",
    token_counter: Callable[[str], int] | None = None,
    route_evidence: Mapping[str, Any] | None = None,
    code_attribution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Query full feedback once, then return agent, audit, diagnosis, or full."""
    if view not in {"agent", "audit", "diagnosis", "full"}:
        raise ValueError("view must be agent, audit, diagnosis, or full")
    from profiler_feedback_sdk import query_profiler_feedback

    full = query_profiler_feedback(
        artifact_roots,
        action_cell=action_cell,
        base_arm=base_arm,
        stat=stat,
        expected_directions=expected_directions,
        token_counter=token_counter,
    )
    if view == "full":
        return full
    if view == "diagnosis":
        return build_profiler_diagnosis(
            full,
            route_evidence=route_evidence,
            code_attribution=code_attribution,
        )
    if view == "audit":
        return build_audit_projection(
            full,
            token_budget=token_budget,
            token_counter=token_counter,
            route_evidence=route_evidence,
            code_attribution=code_attribution,
        )
    return build_agent_projection(
        full,
        token_budget=token_budget,
        token_counter=token_counter,
        route_evidence=route_evidence,
        code_attribution=code_attribution,
    )


def _result_status(payload: Mapping[str, Any], view: str) -> str:
    if view == "audit":
        return str((payload.get("agent") or {}).get("status") or "rejected")
    return str(payload.get("status") or "rejected")


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
    parser.add_argument(
        "--view",
        default="agent",
        choices=("agent", "audit", "diagnosis", "full"),
    )
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    parser.add_argument("--route-evidence", type=Path)
    parser.add_argument("--code-attribution", type=Path)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload = query_profiler_feedback_view(
            args.artifact_root,
            action_cell=args.action_cell,
            base_arm=args.base_arm,
            stat=args.stat,
            token_budget=args.token_budget,
            view=args.view,
            route_evidence=_load_json(args.route_evidence),
            code_attribution=_load_json(args.code_attribution),
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.output.exists():
        print(f"refusing existing output: {args.output}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.view == "full":
        from profiler_feedback_sdk import serialize_feedback

        text = serialize_feedback(payload)
    elif args.view == "diagnosis":
        text = serialize_diagnosis(payload, pretty=args.pretty)
    else:
        text = serialize_projection(payload, pretty=args.pretty)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return {"ok": 0, "partial": 3, "rejected": 4}.get(
        _result_status(payload, args.view), 4
    )


def __getattr__(name: str) -> Any:
    """Forward non-overridden compatibility symbols to the frozen core."""
    return getattr(_core, name)


if __name__ == "__main__":
    raise SystemExit(main())
