"""Scope-first retrieval and compact ContextProjection construction."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from kernelcat_knowledge.contracts.canonical import canonical_json, sha256_digest
from kernelcat_knowledge.contracts.validation import (
    validate_context_projection,
    validate_knowledge_query,
    validate_query_capabilities,
    validate_query_predicates,
)

_MATURITY_RANK = {
    "draft": 0,
    "source_derived": 1,
    "api_valid": 2,
    "compile_tested": 3,
    "run_tested": 4,
    "benchmarked_local": 5,
    "formally_measured": 6,
    "officially_landed": 7,
    "retracted": -1,
}


def _items(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _normalize(value: Any) -> str:
    return re.sub(r"[\s_\-./]+", "", str(value or "").strip().lower())


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_+.-]+|[\u3400-\u9fff]{1,6}", text.lower())
        if len(token) > 1 or token.isdigit()
    }


def estimate_tokens(value: Any) -> int:
    text = canonical_json(value) if not isinstance(value, str) else value
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return max(1, cjk + (max(len(text) - cjk, 0) + 3) // 4)


def _card_scope(card: Mapping[str, Any]) -> dict[str, Any]:
    applicability = card.get("applicability")
    applicability = dict(applicability) if isinstance(applicability, Mapping) else {}
    task = card.get("task")
    task = task if isinstance(task, Mapping) else {}
    aliases = {
        "operator": ("operator", "operator_id"),
        "direction": ("direction",),
        "dtype": ("dtype",),
        "layout": ("layout",),
        "hardware": ("hardware", "architecture", "soc"),
        "toolchain": ("toolchain", "cann"),
        "parent_family": ("parent_family",),
    }
    scope: dict[str, Any] = {}
    for canonical, keys in aliases.items():
        values: list[Any] = []
        for key in keys:
            values.extend(_items(applicability.get(key)))
            values.extend(_items(task.get(key)))
            values.extend(_items(card.get(key)))
        values = [item for item in values if item not in (None, "")]
        if values:
            scope[canonical] = values
    for key, value in applicability.items():
        scope.setdefault(key, value)
    return scope


def _query_scope(query: Mapping[str, Any]) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    if query.get("operator_id"):
        scope["operator"] = query["operator_id"]
    if query.get("mechanism_family"):
        scope["mechanism_family"] = query["mechanism_family"]
    if query.get("hardware"):
        scope["hardware"] = query["hardware"]
    if query.get("toolchain"):
        scope["toolchain"] = query["toolchain"]
    scope.update(validate_query_predicates(query.get("predicates")))
    return scope


def _intersects(expected: Any, available: Any) -> bool:
    wants = {_normalize(item) for item in _items(expected) if item not in (None, "")}
    haves = {_normalize(item) for item in _items(available) if item not in (None, "")}
    if not wants:
        return True
    if not haves or "unknown" in haves:
        return False
    return bool(wants & haves)


def scope_matches(card: Mapping[str, Any], query: Mapping[str, Any]) -> bool:
    if str(card.get("status") or "active") != "active":
        return False
    scope = _card_scope(card)
    requested = _query_scope(query)
    available_capabilities = validate_query_capabilities(query["capabilities"]) if "capabilities" in query else []
    required = {_normalize(item) for item in _items(scope.get("required_capabilities"))}
    observed = {_normalize(item) for item in available_capabilities}
    if "unknown" in required or not required.issubset(observed):
        return False
    for field, expected in requested.items():
        if field in {"shape_predicates", "dispatch_predicates", "required_capabilities"}:
            if not _intersects(expected, scope.get(field)):
                return False
            continue
        if field == "mechanism_family":
            available = card.get("mechanism_family") or card.get("policy_id") or scope.get(field)
        else:
            available = scope.get(field)
        if not _intersects(expected, available):
            return False
    return True


def _search_text(card: Mapping[str, Any]) -> str:
    decision = dict(card.get("decision") or {}) if isinstance(card.get("decision"), Mapping) else {}
    parts = [
        card.get("title"),
        card.get("summary"),
        card.get("decision_question"),
        card.get("mechanism_family"),
        card.get("policy_id"),
        decision.get("bottleneck"),
        *(_items(card.get("tags"))),
        *(_items(card.get("use_when"))),
        *(_items(card.get("reject_when"))),
        *(_items((card.get("applicability") or {}).get("use_when")) if isinstance(card.get("applicability"), Mapping) else []),
        *(_items((card.get("applicability") or {}).get("reject_when")) if isinstance(card.get("applicability"), Mapping) else []),
    ]
    return " ".join(str(item) for item in parts if item not in (None, ""))


def _rank(card: Mapping[str, Any], query: Mapping[str, Any]) -> tuple[int, int, int, str]:
    needle = " ".join(
        str(query.get(key) or "")
        for key in ("operator_id", "mechanism_family", "bottleneck", "decision_question", "device_feedback")
    )
    query_tokens = _tokens(needle)
    haystack = _search_text(card)
    card_tokens = _tokens(haystack)
    lexical = len(query_tokens & card_tokens)
    exact_bottleneck = int(_normalize(query.get("bottleneck")) in _normalize(haystack))
    tombstone = int(card.get("card_type") == "Tombstone" and (lexical > 0 or exact_bottleneck))
    maturity = _MATURITY_RANK.get(str(card.get("evidence_maturity") or "draft"), 0)
    return (-tombstone, -exact_bottleneck, -(lexical * 10 + maturity), str(card.get("card_id") or card.get("ID") or ""))


def _evidence_refs(card: Mapping[str, Any]) -> list[str]:
    refs = [str(item) for item in _items(card.get("evidence_refs")) if item not in (None, "")]
    for claim in _items(card.get("claims")):
        if isinstance(claim, Mapping):
            refs.extend(str(item) for item in _items(claim.get("evidence_refs")) if item not in (None, ""))
    return list(dict.fromkeys(refs))


def _compact_claims(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for claim in _items(card.get("claims"))[:4]:
        if not isinstance(claim, Mapping):
            continue
        claims.append(
            {
                key: deepcopy(claim[key])
                for key in ("claim_id", "subject", "predicate", "object", "unit", "authority_scope", "confidence")
                if key in claim
            }
        )
    return claims


def _compact_card(card: Mapping[str, Any]) -> dict[str, Any]:
    applicability = dict(card.get("applicability") or {}) if isinstance(card.get("applicability"), Mapping) else {}
    use_when = card.get("use_when") or applicability.get("use_when") or []
    reject_when = card.get("reject_when") or applicability.get("reject_when") or []
    decision = dict(card.get("decision") or {}) if isinstance(card.get("decision"), Mapping) else {}
    item: dict[str, Any] = {
        "card_id": str(card.get("card_id") or card.get("ID") or ""),
        "revision_id": str(card.get("revision_id") or ""),
        "card_type": str(card.get("card_type") or "MechanismPolicy"),
        "title": str(card.get("title") or card.get("summary") or ""),
        "summary": str(card.get("summary") or ""),
        "applicability": deepcopy(dict(applicability)),
        "use_when": [str(item) for item in _items(use_when)[:4]],
        "reject_when": [str(item) for item in _items(reject_when)[:4]],
        "claims": _compact_claims(card),
        "evidence_maturity": str(card.get("evidence_maturity") or "draft"),
        "evidence_refs": _evidence_refs(card),
    }
    if decision:
        item["decision"] = {
            key: deepcopy(decision[key])
            for key in ("preferred_variant", "fallback_variant", "bottleneck", "patch_atoms")
            if key in decision
        }
    for key in ("reward_scope", "formal", "attribution_confidence", "revisit_when", "next_evidence_action"):
        if card.get(key) not in (None, "", [], {}):
            item[key] = deepcopy(card[key])
    return item


def _active_revision(cards: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_card: dict[str, list[dict[str, Any]]] = {}
    for raw in cards:
        card = dict(raw)
        if str(card.get("status") or "active") != "active":
            continue
        card_id = str(card.get("card_id") or card.get("ID") or "")
        if not card_id:
            continue
        by_card.setdefault(card_id, []).append(card)
    active: list[dict[str, Any]] = []
    for card_id, revisions in by_card.items():
        # Conflict resolution belongs to the publish path. A projection must not
        # guess between two simultaneously active interpretations.
        unique = {str(item.get("revision_id") or ""): item for item in revisions}
        if len(unique) != 1:
            continue
        active.append(next(iter(unique.values())))
    return active


def _try_add(target: list[dict[str, Any]], item: dict[str, Any], projection: dict[str, Any], budget: int) -> bool:
    target.append(item)
    if estimate_tokens(projection) <= budget:
        return True
    target.pop()
    return False


def build_context_projection(
    query_payload: Mapping[str, Any],
    cards: Sequence[Mapping[str, Any]],
    *,
    active_index_revision: str | None = None,
) -> dict[str, Any]:
    query = validate_knowledge_query(query_payload)
    budget = int(query["token_budget"])
    seen = {str(item) for item in query.get("seen_ids") or []}
    matching = [
        card
        for card in _active_revision(cards)
        if card.get("card_type") != "OperatorHistory"
        and str(card.get("card_id") or card.get("ID") or "") not in seen
        and scope_matches(card, query)
    ]
    matching.sort(key=lambda card: _rank(card, query))
    matching = matching[: query["limit"]]

    scope = _query_scope(query)
    query_id = "q_" + sha256_digest(query).split(":", 1)[1][:20]
    projection: dict[str, Any] = {
        "schema_version": "kernelcat.context-projection/v1",
        "query_id": query_id,
        "active_index_revision": active_index_revision or sha256_digest(
            [
                [str(card.get("card_id") or card.get("ID") or ""), str(card.get("revision_id") or "")]
                for card in _active_revision(cards)
            ]
        ),
        "decision_question": query["decision_question"],
        "scope": scope,
        "must_obey": [],
        "recommended_policies": [],
        "do_not_retry": [],
        "measurement_rules": [],
        "evidence_refs": list(dict.fromkeys(str(item) for item in query.get("evidence_refs") or [])),
        "uncertainties": [],
        "next_evidence_action": "核对适用谓词后，按 correctness gate → 同协议 Event A/B → formal reconcile 的顺序补证据。",
        "token_count": 0,
        "truncated": False,
    }

    buckets = {
        "OfficialFact": projection["must_obey"],
        "MechanismPolicy": projection["recommended_policies"],
        "CausalGain": projection["recommended_policies"],
        "Tombstone": projection["do_not_retry"],
        "PlatformFact": projection["must_obey"],
        "MeasurementProtocol": projection["measurement_rules"],
    }
    for card in matching:
        compact = _compact_card(card)
        bucket = buckets.get(str(card.get("card_type") or "MechanismPolicy"), projection["recommended_policies"])
        if not _try_add(bucket, compact, projection, budget):
            projection["truncated"] = True
            continue
        projection["evidence_refs"] = list(
            dict.fromkeys([*projection["evidence_refs"], *compact.get("evidence_refs", [])])
        )

    if not matching:
        projection["uncertainties"].append("没有 active card 同时满足全部硬 scope；相似文本不得绕过 applicability。")
        projection["next_evidence_action"] = "先采集 task/source/profile 的最小证据，再提交 card candidate；不要让 Agent 从相似算子自行补 scope。"
    elif projection["do_not_retry"] and not projection["recommended_policies"]:
        projection["next_evidence_action"] = "当前机制命中 Tombstone；只有满足 revisit_when 或切换机制/parent family 后才重开。"

    projection["token_count"] = estimate_tokens(projection)
    while projection["token_count"] > budget and projection["uncertainties"]:
        projection["uncertainties"].pop()
        projection["truncated"] = True
        projection["token_count"] = estimate_tokens(projection)
    if projection["token_count"] > budget:
        projection["next_evidence_action"] = "补齐最小证据后重新查询。"
        projection["truncated"] = True
        projection["token_count"] = estimate_tokens(projection)
    if projection["token_count"] > budget:
        # The fixed envelope is intentionally tiny; reaching this branch means
        # the caller supplied abnormally large scope values despite validation.
        raise ValueError("fixed ContextProjection envelope exceeds token budget")
    return validate_context_projection(projection)
