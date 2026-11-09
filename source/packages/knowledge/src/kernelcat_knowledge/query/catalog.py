"""Scoped lexical/read-model search over active Knowledge cards."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Container, Mapping
from time import perf_counter
from typing import Any

from kernelcat_knowledge.cards.active import load_cards, validate_cards
from kernelcat_knowledge.cards.eligibility import FactEligibility
from kernelcat_knowledge.contracts import (
    ContractError,
    sha256_digest,
    validate_context_projection,
    validate_contract,
)
from kernelcat_knowledge.ports import KnowledgeRepository, ReadModel
from kernelcat_knowledge.query.projection import (
    build_context_projection,
    estimate_tokens,
    scope_matches,
)
from kernelcat_knowledge.query.results import finish_result


def _items(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def phase_matches(card: Mapping[str, Any], phase: str) -> bool:
    if phase == "all":
        return True
    declared = card.get("phases") or card.get("phase")
    if declared in (None, "", []):
        return True
    values = {str(item) for item in _items(declared)}
    return phase in values or "all" in values


def decorate_projection(
    projection: dict[str, Any], *, budget: int, backend: str
) -> dict[str, Any]:
    result = dict(projection)
    result["status"] = "ok"
    result["read_model"] = backend
    result["digest"] = sha256_digest(
        {
            key: value
            for key, value in result.items()
            if key not in {"digest", "token_count"}
        }
    )
    result["token_count"] = estimate_tokens(result)
    trim_order = (
        "recommended_policies",
        "measurement_rules",
        "must_obey",
        "do_not_retry",
    )
    changed = False
    while result["token_count"] > budget:
        removed = False
        for field in trim_order:
            if result.get(field):
                result[field].pop()
                removed = True
                changed = True
                break
        if not removed and result.get("uncertainties"):
            result["uncertainties"].pop()
            removed = True
            changed = True
        if not removed and len(result.get("evidence_refs") or []) > 3:
            result["evidence_refs"].pop()
            removed = True
            changed = True
        if not removed:
            result["next_evidence_action"] = "补齐最小证据后重新查询。"
            result["token_count"] = estimate_tokens(result)
            break
        result["token_count"] = estimate_tokens(result)
    if result["token_count"] > budget:
        raise ContractError(
            "ContextProjection fixed envelope exceeds requested token budget"
        )
    if changed:
        result["truncated"] = True
        result["digest"] = sha256_digest(
            {
                key: value
                for key, value in result.items()
                if key not in {"digest", "token_count"}
            }
        )
        result["token_count"] = estimate_tokens(result)
    return validate_context_projection(result)


def _dependency_closure(
    cards: list[dict[str, Any]], scoped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """O(cards + edges) ancestors needed for scoped current-denial decisions.

    Keep cross-scope parents, but do not resolve unrelated cards' corrupt metadata.
    Required metadata errors still reach the caller through the original owner.
    """
    by_identity = {
        identity: card
        for card in cards
        for identity in (card["card_id"], card["revision_id"])
    }
    selected = {card["card_id"] for card in scoped}
    pending = deque(scoped)
    while pending:
        for identity in pending.popleft().get("depends_on", []):
            parent = by_identity.get(identity)
            if parent is not None and parent["card_id"] not in selected:
                selected.add(parent["card_id"])
                pending.append(parent)
    return [card for card in cards if card["card_id"] in selected]


def search(
    request: Mapping[str, Any], *, knowledge: KnowledgeRepository,
    read_model: ReadModel | None,
    eligibility: FactEligibility,
    evidence_membership: Container[str] | None = None,
    binding: dict[str, Any],
    card_snapshot: Callable[[], tuple[list[dict[str, Any]], str]] | None = None,
    denial_filter: Callable[[list[dict[str, Any]]], Container[str]] | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    query = validate_contract(request)
    if query.get("schema_version") != "kernelcat.knowledge-query/v1":
        raise ContractError("wiki search requires kernelcat.knowledge-query/v1")
    if evidence_membership is not None and any(
        evidence_id not in evidence_membership for evidence_id in query.get("evidence_refs", [])
    ):
        raise ContractError("evidence ID is outside retained membership")
    if card_snapshot is None:
        cards, invalid = load_cards(knowledge)
        index_revision = knowledge.active_index_revision
    else:
        entries, index_revision = card_snapshot()
        cards, invalid = validate_cards(entries)
    scoped = [
        card
        for card in cards
        if card.get("card_type") != "OperatorHistory"
        and phase_matches(card, str(query["phase"])) and scope_matches(card, query)
    ]
    if denial_filter is not None:
        denied = denial_filter(_dependency_closure(cards, scoped))
        scoped = [card for card in scoped if card["card_id"] not in denied]
    eligible = []
    failures = []
    for card in scoped:
        reason = eligibility.reason(card)
        if reason is None:
            eligible.append(card)
        else:
            failures.append(reason)
    scoped = eligible
    search_text = " ".join(
        str(query.get(key) or "")
        for key in (
            "operator_id",
            "mechanism_family",
            "phase",
            "bottleneck",
            "decision_question",
            "device_feedback",
        )
    )
    scoped_ids = [str(card.get("card_id") or "") for card in scoped]
    ranked_ids = (
        [] if read_model is None else read_model.search(
            search_text,
            candidate_ids=scoped_ids,
            limit=max(20, int(query["limit"]) * 5),
            expected_revision=index_revision,
        )
    )
    if ranked_ids:
        by_id = {str(card.get("card_id") or ""): card for card in scoped}
        tombstones = [card for card in scoped if card.get("card_type") == "Tombstone"]
        selected = [by_id[card_id] for card_id in ranked_ids if card_id in by_id]
        selected_ids = {str(card.get("card_id")) for card in selected}
        scoped = [
            *tombstones,
            *(
                card
                for card in selected
                if str(card.get("card_id"))
                not in {str(t.get("card_id")) for t in tombstones}
            ),
        ]
        if not scoped:
            scoped = [
                card for card in cards if str(card.get("card_id")) in selected_ids
            ]
        backend = "sqlite-fts5"
    else:
        backend = "deterministic-fallback"
    projection = build_context_projection(
        query,
        scoped,
        active_index_revision=index_revision,
    )
    if failures:
        # Replace the generic action with a compact diagnostic so an unrelated
        # ineligible card cannot consume the budget of a useful matching fact.
        projection["next_evidence_action"] = "Run card validate for full eligibility reasons."
        projection["uncertainties"].append(
            f"{len(failures)} fact(s) ineligible for must_obey: "
            + failures[0].split(":", 1)[0]
        )
    if invalid:
        projection["uncertainties"].append(
            f"{len(invalid)} 个 V1 card 未通过确定性合同，已从默认索引隔离。"
        )
    if query.get("task_envelope_ref"):
        projection["uncertainties"].append(
            "task_envelope_ref 已保留，但本阶段只使用显式 hard predicates；未从引用中猜测 scope。"
        )
    projection = decorate_projection(
        projection, budget=int(query["token_budget"]), backend=backend,
    )
    return finish_result(projection, query, binding, (perf_counter() - started) * 1000)
