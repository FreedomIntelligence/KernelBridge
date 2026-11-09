"""Protocol-independent cann-ask v5.2 compatibility search.

This module preserves the old response contract without allowing the MCP
transport to own a second search implementation. Alias resolution is injected
by the compatibility adapter. Ranking is lexical and deterministic; no provider
or model process is available.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SCORE_FLOOR = 1e-9


def unique_nonempty(values: Sequence[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if value not in (None, "") and str(value).strip()))


def _normalize(value: Any) -> str:
    return re.sub(r"[\s_\-./]+", "", str(value or "").strip().lower())


def _tokens(value: str) -> list[str]:
    latin = re.findall(r"[A-Za-z0-9_+.-]+", value.lower())
    cjk: list[str] = []
    for group in re.findall(r"[\u3400-\u9fff]+", value):
        cjk.extend(group[index:index + width] for width in (2, 3, 4) for index in range(max(0, len(group) - width + 1)))
    return unique_nonempty([*latin, *cjk])


def _flatten(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, Mapping):
        return [text for key, item in value.items() for text in [str(key), *_flatten(item)]]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [text for item in value for text in _flatten(item)]
    return [str(value)]


def lexical_score(query: str, entry: Mapping[str, Any], resolver: Any) -> float:
    expanded = resolver.expand_query(query) if resolver is not None else query
    terms = {_normalize(term) for term in _tokens(expanded) if _normalize(term)}
    if not terms:
        return 0.0
    fields = {
        "identity": " ".join(_flatten(entry.get("ID")) + _flatten(entry.get("title")) + _flatten(entry.get("tags"))),
        "summary": " ".join(_flatten(entry.get("summary"))),
        "content": " ".join(
            _flatten(entry.get("body"))
            + _flatten(entry.get("content"))
            + _flatten(entry.get("task"))
            + _flatten(entry.get("decision"))
            + _flatten(entry.get("applicability"))
        ),
    }
    normalized = {name: _normalize(text) for name, text in fields.items()}
    weights = {"identity": 5.0, "summary": 3.0, "content": 1.0}
    score = sum(
        max((weight for name, weight in weights.items() if term in normalized[name]), default=0.0)
        for term in terms
    )
    return score / max(len(terms), 1)


def quality_bonus(entry: Mapping[str, Any]) -> float:
    bonus = 0.04 if entry.get("trust_contract") == "v1" else 0.0
    evidence = entry.get("evidence") or []
    evidence = evidence if isinstance(evidence, list) else [evidence]
    evidence = [row for row in evidence if isinstance(row, Mapping)]
    if evidence:
        bonus += 0.02
    if any(row.get("kind") in {"official-doc", "upstream-code", "upstream-pr"} for row in evidence):
        bonus += 0.03
    reproducibility = {
        "": 0, "concept": 1, "pseudocode": 2, "api-valid": 3,
        "compile-tested": 4, "run-tested": 5, "benchmarked": 6,
    }.get(str(entry.get("reproducibility") or ""), 0)
    bonus += min(reproducibility, 6) * 0.005
    verification = entry.get("verification")
    if isinstance(verification, Mapping):
        level = int(bool(verification.get("api")))
        level = max(level, 2 * int(bool(verification.get("compile"))))
        level = max(level, 3 * int(bool(verification.get("correctness"))))
        level = max(level, 4 * int(bool(verification.get("benchmark"))))
        bonus += min(level, 4) * 0.015
    return min(bonus, 0.15)


def normalize_phase(value: str | None) -> tuple[str | None, str | None]:
    phase = (value or "correctness").strip().lower()
    if phase not in {"correctness", "precision", "performance", "all"}:
        return None, f"invalid phase {phase!r}; expected correctness/precision/performance/all"
    return phase, None


def build_phase_rule(phase: str) -> dict[str, Any]:
    """Preserve the legacy field without injecting retired instructions."""
    return {
        "phase": phase,
        "content_mode": "suppressed",
        "content": "",
        "description": "Legacy phase rules retired; results are attributed source data.",
        "estimated_tokens": 0,
        "rule_refs": [],
    }


def tier_envelope(results: list[dict[str, Any]], total: int, warning: str | None = None) -> dict[str, Any]:
    return {
        "results": results,
        "total": int(total),
        "no_useful_results": not bool(results),
        "warning": warning,
    }


def response_envelope(
    *,
    phase_rule: dict[str, Any],
    static: dict[str, Any],
    dynamic: dict[str, Any],
    ai_suggestion: str | None,
    agent_status: str,
    agent_turns: int | None,
    sub_queries: list[str] | None,
    warning: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "phase_rule": phase_rule,
        "ai_suggestion": ai_suggestion,
        "agent_status": agent_status,
        "agent_turns": agent_turns,
        "sub_queries": sub_queries,
        "static": static,
        "dynamic": dynamic,
    }
    if warning:
        payload["warning"] = warning
    return payload


def _candidate_filter(entries: Sequence[Mapping[str, Any]], *, tags: list[str] | None, domain: str | None, seen_ids: set[str], resolver: Any) -> list[dict[str, Any]]:
    requested = {resolver.canonicalize(tag) for tag in (tags or [])} if resolver is not None else set(tags or [])
    rows: list[dict[str, Any]] = []
    for raw in entries:
        entry = dict(raw)
        if str(entry.get("ID")) in seen_ids:
            continue
        if domain and entry.get("domain") != domain:
            continue
        if requested:
            have = {
                resolver.canonicalize(tag) if resolver is not None else str(tag)
                for tag in entry.get("tags") or []
            }
            if not requested & have:
                continue
        if entry.get("content_path") or entry.get("body"):
            rows.append(entry)
    return rows


def _combined_query(query: str, resolver: Any, task_description: str | None, device_feedback: str | None) -> tuple[str, list[str]]:
    expanded = resolver.expand_query(query) if resolver is not None else query
    feedback = f"设备反馈: {device_feedback.strip()}" if device_feedback else ""
    task = f"任务: {task_description.strip()}" if task_description else ""
    retrieval = "\n".join(unique_nonempty([expanded, task, feedback]))
    return retrieval, unique_nonempty([query, expanded if expanded != query else None, feedback or None])


def _rank(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    resolver: Any,
) -> list[tuple[dict[str, Any], float, float]]:
    rows: list[tuple[dict[str, Any], float, float]] = []
    for raw in candidates:
        entry = dict(raw)
        relevance = lexical_score(query, entry, resolver)
        if relevance <= _SCORE_FLOOR:
            continue
        score = relevance + quality_bonus(entry)
        rows.append((entry, score, relevance))
    rows.sort(key=lambda row: (-row[1], str(row[0]["ID"])))
    return rows


def _static_result(entry: Mapping[str, Any], score: float, relevance: float, rank: int, intent: str) -> dict[str, Any]:
    q_value = float(entry.get("Q_value") or 0.0)
    q_draft = float(entry.get("Q_draft", q_value) or 0.0)
    q_refine = float(entry.get("Q_refine", q_value) or 0.0)
    q_intent = q_draft if intent == "q1" else q_refine if intent == "q2" else max(q_draft, q_refine)
    return {
        "id": str(entry["ID"]),
        "summary": str(entry.get("summary") or ""),
        "score": float(score),
        "tags": list(entry.get("tags") or []),
        "branch": "traditional",
        "extra": {
            "qValue": q_value,
            "qDraft": q_draft,
            "qRefine": q_refine,
            "qIntent": q_intent,
            "rerankRank": rank,
            "rerankScore": float(score),
            "rrfScore": float(relevance),
        },
    }


def _citation_ids(entry: Mapping[str, Any], loader: Any) -> list[str]:
    result: list[str] = []
    evidence = entry.get("evidence") or []
    evidence = evidence if isinstance(evidence, list) else [evidence]
    for row in evidence:
        if not isinstance(row, Mapping):
            continue
        if row.get("source_id"):
            result.append(str(row["source_id"]))
            continue
        source = str(row.get("source") or "")
        if source.startswith("wiki/"):
            match = loader.find_by_path(source)
            if match:
                result.append(str(match["ID"]))
    return list(dict.fromkeys(result))


def _dynamic_result(entry: Mapping[str, Any], score: float, loader: Any) -> dict[str, Any]:
    applicability = dict(entry.get("applicability") or {}) if isinstance(entry.get("applicability"), Mapping) else {}
    decision = dict(entry.get("decision") or {}) if isinstance(entry.get("decision"), Mapping) else {}
    use_when = list(applicability.get("use_when") or [])
    cautions = [
        *(str(item) for item in applicability.get("reject_when") or []),
        *(str(item) for item in entry.get("risks") or []),
    ]
    return {
        "id": str(entry["ID"]),
        "summary": str(entry.get("summary") or ""),
        "scenario": "；".join(str(item) for item in use_when[:2]) or str(entry.get("summary") or ""),
        "steps_outline": [str(item) for item in decision.get("patch_atoms") or []],
        "cautions": cautions[:8],
        "score": float(score),
        "tags": list(entry.get("tags") or []),
        "branch": "dynamic",
        "citations": _citation_ids(entry, loader),
        "community_id": entry.get("community_id"),
        "neighbors_top3": list(entry.get("neighbors_top3") or [])[:3],
    }


def _intent_for(phase: str, explicit: str | None) -> tuple[str | None, str | None]:
    derived = "q2" if phase == "performance" else "mixed" if phase == "all" else "q1"
    if explicit and explicit not in {"q1", "q2", "mixed"}:
        return None, f"invalid intent {explicit!r}; expected q1/q2/mixed"
    return explicit or derived, None


async def search_enveloped(
    query: str,
    phase: str | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
    limit: int = 10,
    intent: str | None = None,
    progress: str | None = None,
    task_description: str | None = None,
    call_count: int = 0,
    seen_ids: list[str] | None = None,
    device_feedback: str | None = None,
    *,
    mode: str,
    loader: Any,
    resolver: Any,
) -> dict[str, Any]:
    if mode != "lexical":
        raise ValueError(f"unsupported search mode {mode!r}; expected 'lexical'")
    effective_phase, phase_error = normalize_phase(phase if phase is not None else progress)
    if phase_error:
        warning = phase_error
        return response_envelope(
            phase_rule=build_phase_rule("correctness"),
            ai_suggestion=None,
            agent_status="disabled",
            agent_turns=None,
            sub_queries=None,
            static=tier_envelope([], 0, warning),
            dynamic=tier_envelope([], 0, warning),
            warning=warning,
        )
    assert effective_phase is not None
    effective_intent, intent_error = _intent_for(effective_phase, intent)
    if intent_error:
        return response_envelope(
            phase_rule=build_phase_rule(effective_phase),
            ai_suggestion=None,
            agent_status="disabled",
            agent_turns=None,
            sub_queries=None,
            static=tier_envelope([], 0, intent_error),
            dynamic=tier_envelope([], 0, intent_error),
            warning=intent_error,
        )
    assert effective_intent is not None
    phase_rule = build_phase_rule(effective_phase)
    if not query or not query.strip():
        warning = "empty query — please supply non-empty search text"
        return response_envelope(
            phase_rule=phase_rule,
            ai_suggestion=None,
            agent_status="disabled",
            agent_turns=None,
            sub_queries=None,
            static=tier_envelope([], 0, warning),
            dynamic=tier_envelope([], 0, warning),
            warning=warning,
        )

    seen = {str(item) for item in (seen_ids or [])}
    retrieval_query, sub_queries = _combined_query(query, resolver, task_description, device_feedback)
    static_candidates = _candidate_filter(
        loader.load_tier("static"), tags=tags, domain=type, seen_ids=seen, resolver=resolver
    )
    dynamic_candidates = _candidate_filter(
        loader.load_tier("dynamic"), tags=tags, domain=type, seen_ids=seen, resolver=resolver
    )
    static_ranked = _rank(
        retrieval_query,
        static_candidates,
        resolver=resolver,
    )
    dynamic_ranked = _rank(
        retrieval_query,
        dynamic_candidates,
        resolver=resolver,
    )
    static_results = [
        _static_result(entry, score, relevance, rank, effective_intent)
        for rank, (entry, score, relevance) in enumerate(
            static_ranked[: max(0, min(int(limit), 10))], 1
        )
    ]
    dynamic_results = [
        _dynamic_result(entry, score, loader)
        for entry, score, _ in dynamic_ranked[: max(0, min(int(limit), 2))]
    ]
    load_errors = unique_nonempty(loader.load_errors)
    load_warning = " | ".join(load_errors) if load_errors else None
    return response_envelope(
        phase_rule=phase_rule,
        ai_suggestion=None,
        agent_status="disabled",
        agent_turns=None,
        sub_queries=sub_queries,
        static=tier_envelope(static_results, len(static_candidates), load_warning),
        dynamic=tier_envelope(dynamic_results, len(dynamic_candidates), load_warning),
        warning=load_warning,
    )
