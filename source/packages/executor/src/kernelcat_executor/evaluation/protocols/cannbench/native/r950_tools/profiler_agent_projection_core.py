#!/usr/bin/env python3
"""Build a token-budgeted Agent view from completed profiler feedback.

The existing :mod:`profiler_feedback_sdk` output is deliberately self-contained
and audit-friendly: it embeds every recognised JSON payload. That is the right
format for archival review, but it is the wrong default context for a coding
agent. This module keeps the audit output untouched and derives a small,
deterministic projection from it.

The extractor uses greedy budgeted maximum coverage. Decision-critical facts
(Event reward, correctness, provenance, and every caveat code) are mandatory.
Optional evidence atoms are selected by information value per incremental token
while rewarding new coverage and penalising redundancy. No language model is
used to decide which evidence survives.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


AGENT_SCHEMA_VERSION = "ascend-profiler-feedback-agent/v1"
AUDIT_SCHEMA_VERSION = "ascend-profiler-feedback-audit/v1"
EXTRACTION_ALGORITHM = "greedy-budgeted-maximum-coverage/v1"
DEFAULT_TOKEN_BUDGET = 1200


@dataclass(frozen=True)
class EvidenceAtom:
    """One optional evidence item considered by the budgeted selector."""

    key: str
    section: str
    payload: Mapping[str, Any]
    value: float
    coverage: frozenset[str]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1].strip()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _case_index(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else None


def _compact_number(value: Any, digits: int = 6) -> float | int | None:
    number = _number(value)
    if number is None:
        return None
    rounded = round(number, digits)
    if rounded == 0:
        rounded = 0.0
    if float(rounded).is_integer() and abs(rounded) < 10**12:
        return int(rounded)
    return rounded


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and value != {} and value != []:
        target[key] = value


def _packed(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def serialize_projection(payload: Mapping[str, Any], *, pretty: bool = False) -> str:
    """Serialize a projection; compact output is the token-budget authority."""
    if pretty:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return _packed(payload) + "\n"


def _resolve_counter(
    token_counter: Callable[[str], int] | None,
) -> tuple[Callable[[str], int], str, bool]:
    if token_counter is not None:
        return token_counter, "custom", True
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("o200k_base")
        return lambda text: len(encoding.encode(text)), "tiktoken:o200k_base", True
    except Exception:  # pragma: no cover - depends on optional installation
        return (
            lambda text: max(1, (len(text.encode("utf-8")) + 3) // 4),
            "utf8-bytes/4",
            False,
        )


def _token_count(payload: Mapping[str, Any], counter: Callable[[str], int]) -> int:
    return counter(serialize_projection(payload))


def _stabilize_token_count(
    projection: dict[str, Any], counter: Callable[[str], int]
) -> int:
    extraction = projection.setdefault("extraction", {})
    for _ in range(12):
        measured = _token_count(projection, counter)
        if extraction.get("tokens") == measured:
            return measured
        extraction["tokens"] = measured
    return _token_count(projection, counter)


def _sample_counts(
    sample_counts: Mapping[str, Any], base_arm: str, candidate_arm: str, case_id: int
) -> dict[str, int]:
    result: dict[str, int] = {}
    for role, arm in (("base", base_arm), ("candidate", candidate_arm)):
        raw = sample_counts.get(f"{arm}:{case_id}")
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            result[role] = count
    return result


def _timing_pair_rank(pair: Mapping[str, Any]) -> tuple[int, int, int, int, int, float]:
    protocol = pair.get("protocol") or {}
    samples = pair.get("samples") or {}
    minimum_samples = min((int(v) for v in samples.values()), default=0)
    lcb = _number(pair.get("lcb95")) or float("-inf")
    return (
        int(pair.get("accepted") is True),
        int(pair.get("stable") is True),
        int(not bool(protocol.get("cache_clean_each_sample"))),
        int(protocol.get("inner_repeat") or 1),
        minimum_samples,
        lcb,
    )


def _collect_timing_pairs(feedback: Mapping[str, Any]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    timing = feedback.get("timing") or {}
    for analysis in timing.get("analyses") or []:
        if not isinstance(analysis, Mapping):
            continue
        capture = analysis.get("capture_metadata") or {}
        sample_counts = capture.get("sample_counts") or {}
        base_arm = str(analysis.get("base") or "base")
        for candidate_arm, arm_result in sorted((analysis.get("diff") or {}).items()):
            for row in (arm_result or {}).get("rows") or []:
                case_id = _case_index(row.get("idx"))
                if case_id is None:
                    continue
                protocol: dict[str, Any] = {}
                for field in (
                    "timer",
                    "inner_repeat",
                    "timing_scope",
                    "cache_clean_each_sample",
                    "warmup",
                    "device",
                ):
                    _put(protocol, field, capture.get(field))
                protocol["stat"] = analysis.get("timing_stat") or (
                    (feedback.get("query") or {}).get("timing_stat")
                )
                pair: dict[str, Any] = {
                    "pair_id": row.get("pair_id"),
                    "run_id": analysis.get("run_id"),
                    "operator": analysis.get("op"),
                    "case_id": case_id,
                    "base_arm": base_arm,
                    "candidate_arm": str(candidate_arm),
                    "base_us": _compact_number(row.get("base_us"), 3),
                    "candidate_us": _compact_number(row.get("mt_us"), 3),
                    "speedup": _compact_number(row.get("x"), 4),
                    "lcb95": _compact_number(row.get("speedup_95pct_lcb"), 4),
                    "stable": row.get("stable") is True,
                    "accepted": row.get("accepted") is True,
                    "protocol": protocol,
                    "samples": _sample_counts(
                        sample_counts, base_arm, str(candidate_arm), case_id
                    ),
                    "refs": sorted(set(analysis.get("artifact_refs") or [])),
                    "provenance": dict(analysis.get("provenance") or {}),
                }
                pairs.append(pair)
    return pairs


def _logical_pair_key(pair: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(pair.get("run_id") or ""),
        str(pair.get("operator") or "").casefold(),
        int(pair.get("case_id") or -1),
        str(pair.get("candidate_arm") or ""),
    )


def _select_authoritative_pairs(
    pairs: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, int, str], list[dict[str, Any]]] = {}
    for pair in pairs:
        groups.setdefault(_logical_pair_key(pair), []).append(pair)
    selected: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for key in sorted(groups):
        ordered = sorted(
            groups[key],
            key=lambda item: (_timing_pair_rank(item), str(item.get("pair_id"))),
        )
        selected.append(ordered[-1])
        history.extend(ordered[:-1])
    return selected, history


def _performance_status(rewards: Sequence[Mapping[str, Any]]) -> str:
    if any(item.get("accepted") is True for item in rewards):
        return "candidate_wins"
    if any(item.get("stable") is True for item in rewards):
        return "no_accepted_win"
    if rewards:
        return "inconclusive"
    return "missing"


def _correctness_payload(feedback: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    correctness = feedback.get("correctness") or {}
    failed = int(correctness.get("failed") or 0)
    unknown = int(correctness.get("unknown") or 0)
    complete = correctness.get("complete") is True
    all_passed = correctness.get("all_passed") is True
    if failed:
        status = "failed"
    elif complete and all_passed:
        status = "complete_pass"
    elif correctness.get("cases"):
        status = "incomplete"
    else:
        status = "missing"
    payload: dict[str, Any] = {
        "status": status,
        "complete": complete,
        "cases": int(correctness.get("cases") or 0),
        "passed": int(correctness.get("passed") or 0),
        "failed": failed,
        "unknown": unknown,
    }
    exceptional_arms = []
    artifact_refs: set[str] = set()
    for arm in correctness.get("arms") or []:
        artifact_refs.update(str(item) for item in arm.get("artifact_refs") or [])
        if arm.get("failed") or arm.get("unknown") or not arm.get("all_passed"):
            exceptional_arms.append(
                {
                    "run_id": arm.get("run_id"),
                    "arm": arm.get("arm"),
                    "passed": int(arm.get("passed") or 0),
                    "failed": int(arm.get("failed") or 0),
                    "unknown": int(arm.get("unknown") or 0),
                    "refs": sorted(set(arm.get("artifact_refs") or [])),
                }
            )
    _put(payload, "exceptional_arms", exceptional_arms)
    _put(payload, "refs", sorted(artifact_refs))
    if correctness.get("missing"):
        payload["missing_count"] = len(correctness.get("missing") or [])
        payload["missing"] = list(correctness.get("missing") or [])[:8]
    return status, payload


def _mechanism_status(feedback: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    verdicts = []
    selected_pair_refs = []
    artifact_refs: set[str] = set()
    for comparison in ((feedback.get("pmu") or {}).get("comparisons") or []):
        direction = comparison.get("expected_direction") or {}
        verdicts.append(str(direction.get("verdict") or "unknown"))
        if comparison.get("selected_timing_pair_ref"):
            selected_pair_refs.append(comparison["selected_timing_pair_ref"])
        artifact_refs.update(
            str(item) for item in comparison.get("base_artifact_refs") or []
        )
        artifact_refs.update(
            str(item) for item in comparison.get("candidate_artifact_refs") or []
        )
    if not verdicts:
        status = "missing"
    elif any(value == "not_supported" for value in verdicts):
        status = "not_supported"
    elif any(value == "mixed" for value in verdicts):
        status = "mixed"
    elif any(value == "supported" for value in verdicts):
        status = "supported"
    else:
        status = "inconclusive"
    payload = {
        "status": status,
        "role": "mechanism_only_not_reward",
        "comparisons": len(verdicts),
    }
    _put(payload, "pair_refs", sorted(set(selected_pair_refs)))
    _put(payload, "refs", sorted(artifact_refs))
    if verdicts:
        payload["verdict_counts"] = {
            verdict: verdicts.count(verdict) for verdict in sorted(set(verdicts))
        }
    return status, payload


def _selected_run_ids(rewards: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(item.get("run_id")) for item in rewards if item.get("run_id")}


def _provenance_payload(
    feedback: Mapping[str, Any], rewards: Sequence[Mapping[str, Any]]
) -> tuple[str, dict[str, Any]]:
    selected_runs = _selected_run_ids(rewards)
    conflict_codes = {
        str(item.get("code") or "") for item in feedback.get("rejections") or []
    }
    conflict = "conflicting_run_metadata" in conflict_codes
    contexts = [
        item
        for item in feedback.get("run_contexts") or []
        if not selected_runs or str(item.get("run_id") or "") in selected_runs
    ]
    source_hashes: dict[str, Any] = {}
    for context in contexts:
        hashes = context.get("source_tree_sha256")
        if isinstance(hashes, Mapping) and hashes:
            source_hashes[str(context.get("run_id") or "unknown")] = dict(hashes)
    if conflict:
        status = "conflict"
    elif selected_runs and selected_runs <= set(source_hashes):
        status = "verified"
    elif source_hashes and not selected_runs:
        status = "verified"
    else:
        status = "legacy_missing_source_identity"

    cells = {
        tuple(sorted((item.get("provenance") or {}).items()))
        for item in rewards
        if item.get("provenance")
    }
    payload: dict[str, Any] = {"status": status}
    if cells:
        payload["cells"] = [dict(cell) for cell in sorted(cells)]
    _put(payload, "source_tree_sha256", source_hashes)
    devices = sorted(
        {
            str((item.get("protocol") or {}).get("device"))
            for item in rewards
            if (item.get("protocol") or {}).get("device") is not None
        }
    )
    _put(payload, "devices", devices)
    return status, payload


def _issue_digest(item: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def _compact_issue(kind: str, item: Mapping[str, Any]) -> dict[str, Any]:
    message = str(item.get("message") or "")
    if len(message) > 320:
        message = message[:317] + "..."
    result: dict[str, Any] = {
        "kind": kind,
        "code": str(item.get("code") or "unknown"),
        "digest": _issue_digest(item),
    }
    _put(result, "message", message)
    for source, target in (
        ("artifact_refs", "refs"),
        ("timing_pair_refs", "pair_refs"),
        ("base_artifact_refs", "base_refs"),
        ("candidate_artifact_refs", "candidate_refs"),
    ):
        if item.get(source):
            result[target] = sorted(set(item.get(source) or []))
    if item.get("missing"):
        missing = list(item.get("missing") or [])
        result["missing_count"] = len(missing)
        result["missing"] = missing[:8]
    return result


def _collect_caveats(feedback: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for field, kind in (
        ("rejections", "rejection"),
        ("limitations", "limitation"),
        ("notices", "notice"),
    ):
        for item in feedback.get(field) or []:
            if isinstance(item, Mapping):
                result.append(_compact_issue(kind, item))
    return sorted(result, key=lambda item: (item["kind"], item["code"], item["digest"]))


def _recommendation(performance: str, correctness: str, provenance: str) -> str:
    if correctness == "failed":
        return "reject"
    if performance == "candidate_wins":
        if correctness != "complete_pass":
            return "hold_for_correctness"
        if provenance != "verified":
            return "hold_for_source_identity"
        return "promote"
    if performance == "inconclusive":
        return "remeasure"
    if performance == "no_accepted_win":
        return "do_not_promote"
    return "collect_evidence"


def _history_atom(pair: Mapping[str, Any]) -> EvidenceAtom:
    protocol = pair.get("protocol") or {}
    payload = {
        "pair_id": pair.get("pair_id"),
        "run_id": pair.get("run_id"),
        "operator": pair.get("operator"),
        "case_id": pair.get("case_id"),
        "candidate_arm": pair.get("candidate_arm"),
        "inner_repeat": protocol.get("inner_repeat"),
        "timing_scope": protocol.get("timing_scope"),
        "cache_clean_each_sample": protocol.get("cache_clean_each_sample"),
        "base_us": pair.get("base_us"),
        "candidate_us": pair.get("candidate_us"),
        "speedup": pair.get("speedup"),
        "lcb95": pair.get("lcb95"),
        "stable": pair.get("stable"),
        "accepted": pair.get("accepted"),
        "samples": pair.get("samples"),
        "refs": pair.get("refs"),
    }
    value = 26.0
    if pair.get("accepted"):
        value += 20.0
    if pair.get("stable") is False:
        value += 10.0
    coverage = frozenset(
        {
            "timing_history",
            f"run:{pair.get('run_id')}",
            f"case:{pair.get('operator')}:{pair.get('case_id')}",
            f"protocol:inner={protocol.get('inner_repeat')}",
            f"stability:{pair.get('stable')}",
        }
    )
    return EvidenceAtom(
        key=f"history:{pair.get('pair_id')}",
        section="timing_history",
        payload=payload,
        value=value,
        coverage=coverage,
    )


def _mechanism_atoms(feedback: Mapping[str, Any]) -> list[EvidenceAtom]:
    atoms: list[EvidenceAtom] = []
    comparisons = (feedback.get("pmu") or {}).get("comparisons") or []
    for comparison_index, comparison in enumerate(comparisons):
        base = comparison.get("base") or {}
        candidate = comparison.get("candidate") or {}
        relative = comparison.get("relative_delta") or {}
        direction = comparison.get("expected_direction") or {}
        verdict = str(direction.get("verdict") or "unknown")
        checks = {
            str(item.get("field")): item
            for item in direction.get("checks") or []
            if item.get("field")
        }
        for field in sorted(set(base) & set(candidate)):
            base_value = _number(base.get(field))
            candidate_value = _number(candidate.get(field))
            if base_value is None or candidate_value is None:
                continue
            delta = _number(relative.get(field))
            check = checks.get(str(field)) or {}
            expected = check.get("expected")
            matches = check.get("matches") is True
            both_zero = abs(base_value) <= 1e-12 and abs(candidate_value) <= 1e-12
            # Constant zero counters do not explain a mechanism. Keep a flat
            # field only when the action-cell contract explicitly expected it
            # to move, because that contradiction is decision-relevant.
            if both_zero and expected not in {"increase", "decrease"}:
                continue
            value = 12.0
            if expected in {"increase", "decrease"}:
                value += 55.0
            if matches:
                value += 24.0
            if verdict == "supported":
                value += 14.0
            if delta is not None:
                value += min(32.0, 11.0 * math.log1p(abs(delta)))
            if str(field).endswith("wait_time_us"):
                value += 8.0
            if both_zero:
                value -= 50.0
            group = str(field).split(".", 1)[0]
            payload: dict[str, Any] = {
                "pair_ref": comparison.get("selected_timing_pair_ref"),
                "field": field,
                "base": _compact_number(base_value, 6),
                "candidate": _compact_number(candidate_value, 6),
                "delta": _compact_number(delta, 4),
            }
            _put(payload, "expected", expected)
            _put(payload, "observed", check.get("observed"))
            if expected in {"increase", "decrease"}:
                payload["matches"] = matches
            coverage = {
                f"pmu_pair:{comparison.get('selected_timing_pair_ref')}",
                f"metric_group:{group}",
                f"field:{field}",
                (
                    "effect:positive"
                    if (delta or 0) > 0
                    else "effect:negative"
                    if (delta or 0) < 0
                    else "effect:flat"
                ),
            }
            if expected:
                coverage.add(f"expected:{expected}")
            atoms.append(
                EvidenceAtom(
                    key=f"mechanism:{comparison_index}:{field}",
                    section="mechanism_signals",
                    payload=payload,
                    value=value,
                    coverage=frozenset(coverage),
                )
            )
    return atoms


def _execution_atoms(feedback: Mapping[str, Any]) -> list[EvidenceAtom]:
    atoms: list[EvidenceAtom] = []
    for item_index, item in enumerate(feedback.get("compact_feedback") or []):
        compact = item.get("feedback") or {}
        dictionary = compact.get("kernel_dictionary") or {}
        for case_index, case in enumerate(compact.get("cases") or []):
            execution = case.get("execution") or {}
            kernels = []
            for kernel in execution.get("top_kernels") or []:
                kernel_id = kernel.get("id")
                kernels.append(
                    {
                        "name": dictionary.get(kernel_id, kernel_id),
                        "us": _compact_number(kernel.get("us"), 3),
                        "share": _compact_number(kernel.get("share"), 4),
                    }
                )
            payload: dict[str, Any] = {
                "run_id": item.get("run_id"),
                "arm": item.get("arm"),
                "operator": case.get("operator"),
                "case_id": case.get("case_id"),
                "kernel_count": int(execution.get("kernel_count") or 0),
            }
            _put(
                payload,
                "kernel_total_us",
                _compact_number(execution.get("kernel_total_us"), 3),
            )
            _put(
                payload,
                "launch_bound_ratio",
                _compact_number(execution.get("launch_bound_ratio"), 4),
            )
            _put(payload, "top_kernels", kernels)
            kernel_count = int(execution.get("kernel_count") or 0)
            launch_bound = _number(execution.get("launch_bound_ratio")) or 0.0
            value = 8.0 + min(kernel_count, 8) * 2.0 + min(launch_bound, 1.0) * 35.0
            coverage = frozenset(
                {
                    "execution",
                    f"run:{item.get('run_id')}",
                    f"arm:{item.get('arm')}",
                    f"case:{case.get('operator')}:{_case_index(case.get('case_id'))}",
                }
            )
            atoms.append(
                EvidenceAtom(
                    key=f"execution:{item_index}:{case_index}",
                    section="execution",
                    payload=payload,
                    value=value,
                    coverage=coverage,
                )
            )
    return atoms


def _adoption_atom(feedback: Mapping[str, Any]) -> EvidenceAtom | None:
    adoption = feedback.get("adoption_evidence") or {}
    if not adoption:
        return None
    decision = adoption.get("decision") or {}
    dataset = adoption.get("dataset") or {}
    payload = {
        "adopt": decision.get("adopt") is True,
        "selected_fields": list(decision.get("selected_fields") or []),
        "reasons": list(decision.get("reasons") or []),
        "records": int(dataset.get("records") or 0),
        "operator_groups": int(dataset.get("groups") or 0),
        "label_counts": dict(dataset.get("label_counts") or {}),
    }
    value = 62.0 if payload["adopt"] else 28.0
    return EvidenceAtom(
        key="adoption:summary",
        section="adoption",
        payload=payload,
        value=value,
        coverage=frozenset({"adoption", f"adopt:{payload['adopt']}"}),
    )


def _used_ref_ids(projection: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {"refs", "pair_refs", "base_refs", "candidate_refs"} and isinstance(
                    child, list
                ):
                    result.update(str(item) for item in child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(projection)
    return result


def _ref_index_atom(
    feedback: Mapping[str, Any], projection: Mapping[str, Any]
) -> EvidenceAtom | None:
    wanted = _used_ref_ids(projection)
    refs = []
    for ref in (feedback.get("artifacts") or {}).get("refs") or []:
        if wanted and str(ref.get("id")) not in wanted:
            continue
        refs.append(
            {
                "id": ref.get("id"),
                "kind": ref.get("kind"),
                "sha256": ref.get("sha256"),
            }
        )
    if not refs:
        return None
    return EvidenceAtom(
        key="refs:index",
        section="ref_index",
        payload={"refs": refs},
        value=42.0,
        coverage=frozenset({"audit_refs", *(f"ref:{item['id']}" for item in refs)}),
    )


def _base_coverage(projection: Mapping[str, Any]) -> set[str]:
    covered = {
        f"decision:{key}:{value}"
        for key, value in (projection.get("decision") or {}).items()
    }
    for reward in projection.get("rewards") or []:
        covered.add(f"run:{reward.get('run_id')}")
        covered.add(f"case:{reward.get('operator')}:{reward.get('case_id')}")
        covered.add(f"pmu_pair:{reward.get('pair_id')}")
    for caveat in projection.get("caveats") or []:
        covered.add(f"caveat:{caveat.get('code')}")
    return covered


def _append_atom(projection: dict[str, Any], atom: EvidenceAtom) -> None:
    evidence = projection.setdefault("evidence", {})
    evidence.setdefault(atom.section, []).append(dict(atom.payload))


def _candidate_with_atom(
    projection: Mapping[str, Any],
    atom: EvidenceAtom,
    *,
    selected_count: int,
    remaining_count: int,
    counter: Callable[[str], int],
) -> dict[str, Any]:
    candidate = copy.deepcopy(projection)
    _append_atom(candidate, atom)
    extraction = candidate["extraction"]
    extraction["selected_optional"] = selected_count
    extraction["omitted_optional"] = remaining_count
    _stabilize_token_count(candidate, counter)
    return candidate


def _select_optional_atoms(
    projection: dict[str, Any],
    atoms: Sequence[EvidenceAtom],
    *,
    token_budget: int,
    counter: Callable[[str], int],
) -> dict[str, Any]:
    remaining = list(atoms)
    covered = _base_coverage(projection)
    selected: list[EvidenceAtom] = []
    _stabilize_token_count(projection, counter)

    while remaining:
        current_tokens = int(projection["extraction"].get("tokens") or 0)
        candidates = []
        for atom in remaining:
            candidate = _candidate_with_atom(
                projection,
                atom,
                selected_count=len(selected) + 1,
                remaining_count=len(remaining) - 1,
                counter=counter,
            )
            candidate_tokens = int(candidate["extraction"].get("tokens") or 0)
            if candidate_tokens > token_budget:
                continue
            incremental = max(1, candidate_tokens - current_tokens)
            new_coverage = atom.coverage - covered
            overlap = atom.coverage & covered
            utility = atom.value + 9.0 * len(new_coverage) - 2.5 * len(overlap)
            density = utility / incremental
            candidates.append((density, utility, -incremental, atom.key, atom, candidate))
        if not candidates:
            break
        _, _, _, _, atom, candidate = max(candidates)
        projection = candidate
        selected.append(atom)
        covered.update(atom.coverage)
        remaining = [item for item in remaining if item.key != atom.key]

    extraction = projection["extraction"]
    extraction["selected_optional"] = len(selected)
    extraction["omitted_optional"] = len(atoms) - len(selected)
    extraction["selected_sections"] = {
        section: sum(item.section == section for item in selected)
        for section in sorted({item.section for item in selected})
    }
    extraction["budget_satisfied"] = True
    final_tokens = _stabilize_token_count(projection, counter)
    if final_tokens > token_budget:
        # Counts can gain a token when their decimal width changes. Remove the
        # latest, lowest-marginal selection until the exact serialized result fits.
        while selected and final_tokens > token_budget:
            removed = selected.pop()
            section = (projection.get("evidence") or {}).get(removed.section) or []
            if section:
                section.pop()
            if not section:
                (projection.get("evidence") or {}).pop(removed.section, None)
            extraction["selected_optional"] = len(selected)
            extraction["omitted_optional"] = len(atoms) - len(selected)
            extraction["selected_sections"] = {
                section: sum(item.section == section for item in selected)
                for section in sorted({item.section for item in selected})
            }
            final_tokens = _stabilize_token_count(projection, counter)
    if final_tokens > token_budget:
        extraction["budget_satisfied"] = False
        extraction["mandatory_overflow_tokens"] = final_tokens - token_budget
        _stabilize_token_count(projection, counter)
    if not projection.get("evidence"):
        projection.pop("evidence", None)
    return projection


def build_agent_projection(
    feedback: Mapping[str, Any],
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    token_counter: Callable[[str], int] | None = None,
) -> dict[str, Any]:
    """Extract the highest-value Agent context under an exact token budget.

    The token budget applies to compact JSON including the extraction metadata.
    Mandatory decision facts are never removed. If those facts alone exceed the
    budget, the output is returned with ``budget_satisfied=false`` rather than
    silently dropping correctness, provenance, or caveats.
    """
    if token_budget < 128:
        raise ValueError("token_budget must be at least 128")
    counter, counter_name, exact_counter = _resolve_counter(token_counter)
    all_pairs = _collect_timing_pairs(feedback)
    rewards, history = _select_authoritative_pairs(all_pairs)
    performance = _performance_status(rewards)
    correctness_status, correctness = _correctness_payload(feedback)
    mechanism_status, mechanism = _mechanism_status(feedback)
    provenance_status, provenance = _provenance_payload(feedback, rewards)
    caveats = _collect_caveats(feedback)

    projection: dict[str, Any] = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "source_schema_version": feedback.get("schema_version"),
        "status": feedback.get("status"),
        "query": {
            "action_cell": (feedback.get("query") or {}).get("action_cell"),
            "base_arm": (feedback.get("query") or {}).get("base_arm"),
            "timing_stat": (feedback.get("query") or {}).get("timing_stat"),
        },
        "decision": {
            "performance": performance,
            "correctness": correctness_status,
            "mechanism": mechanism_status,
            "provenance": provenance_status,
            "recommendation": _recommendation(
                performance, correctness_status, provenance_status
            ),
            "pmu_required_for_reward": False,
        },
        "rewards": [
            {key: value for key, value in reward.items() if key != "provenance"}
            for reward in rewards
        ],
        "correctness": correctness,
        "mechanism": mechanism,
        "provenance": provenance,
        "caveats": caveats,
        "extraction": {
            "algorithm": EXTRACTION_ALGORITHM,
            "token_budget": token_budget,
            "counter": counter_name,
            "exact_counter": exact_counter,
            "mandatory_tokens": 0,
            "selected_optional": 0,
            "omitted_optional": 0,
            "tokens": 0,
        },
    }

    if not rewards:
        projection["rewards"] = []
    if not caveats:
        projection.pop("caveats")

    mandatory_tokens = _stabilize_token_count(projection, counter)
    projection["extraction"]["mandatory_tokens"] = mandatory_tokens
    mandatory_tokens = _stabilize_token_count(projection, counter)
    projection["extraction"]["mandatory_tokens"] = mandatory_tokens

    atoms: list[EvidenceAtom] = []
    atoms.extend(_history_atom(pair) for pair in history)
    atoms.extend(_mechanism_atoms(feedback))
    atoms.extend(_execution_atoms(feedback))
    adoption = _adoption_atom(feedback)
    if adoption is not None:
        atoms.append(adoption)
    ref_index = _ref_index_atom(feedback, projection)
    if ref_index is not None:
        atoms.append(ref_index)
    atoms.sort(key=lambda item: item.key)
    projection["extraction"]["omitted_optional"] = len(atoms)
    return _select_optional_atoms(
        projection,
        atoms,
        token_budget=token_budget,
        counter=counter,
    )


def build_audit_projection(
    feedback: Mapping[str, Any],
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    token_counter: Callable[[str], int] | None = None,
) -> dict[str, Any]:
    """Return Agent context plus an evidence index, without embedded payloads."""
    agent = build_agent_projection(
        feedback, token_budget=token_budget, token_counter=token_counter
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
) -> dict[str, Any]:
    """Run the existing one-shot SDK, then return agent, audit or full."""
    if view not in {"agent", "audit", "full"}:
        raise ValueError("view must be agent, audit, or full")
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
    if view == "audit":
        return build_audit_projection(
            full, token_budget=token_budget, token_counter=token_counter
        )
    return build_agent_projection(
        full, token_budget=token_budget, token_counter=token_counter
    )


def _result_status(payload: Mapping[str, Any], view: str) -> str:
    if view == "audit":
        return str((payload.get("agent") or {}).get("status") or "rejected")
    return str(payload.get("status") or "rejected")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", action="append", required=True, type=Path)
    parser.add_argument("--action-cell", required=True)
    parser.add_argument("--base-arm", default="base")
    parser.add_argument("--stat", default="p50", choices=("p50", "p95", "p99"))
    parser.add_argument("--view", default="agent", choices=("agent", "audit", "full"))
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
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
    else:
        text = serialize_projection(payload, pretty=args.pretty)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return {"ok": 0, "partial": 3, "rejected": 4}.get(
        _result_status(payload, args.view), 4
    )


if __name__ == "__main__":
    raise SystemExit(main())
