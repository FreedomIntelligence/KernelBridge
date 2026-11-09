"""Existing card validation, diff and reviewed publication rules."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from kernelcat_knowledge.cards.authority import (
    card_owner_roots,
    card_roots,
    existing_card,
)
from kernelcat_knowledge.cards.eligibility import FactEligibility
from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    validate_contract,
    validate_knowledge_card,
)
from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.ports import KnowledgeRepository
from kernelcat_knowledge.resources.binding import ResourceBinding
from kernelcat_knowledge.storage.catalog_io import catalog_locks, install

_REVIEW_REQUIRED = {
    "MechanismPolicy",
    "CausalGain",
    "Tombstone",
    "PlatformFact",
    "MeasurementProtocol",
    "OperatorHistory",
}


def _public(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not str(key).startswith("_")}


def card_validate(
    card: Mapping[str, Any], *, eligibility: FactEligibility
) -> dict[str, Any]:
    normalized = validate_contract(card)
    if normalized.get("schema_version") != "kernelcat.knowledge-card/v1":
        raise ContractError("card validate requires kernelcat.knowledge-card/v1")
    reason = eligibility.reason(normalized)
    if reason is not None:
        return response(
            "kernelcat.card-operation/v1", "error",
            code="TRUST_GATE_FAILED", message=reason,
            card_id=normalized["card_id"], revision_id=normalized["revision_id"],
        )
    return response(
        "kernelcat.card-operation/v1",
        "valid",
        card_id=normalized["card_id"],
        revision_id=normalized["revision_id"],
        card=normalized,
    )


def card_diff(
    card: Mapping[str, Any], *, knowledge: KnowledgeRepository
) -> dict[str, Any]:
    normalized = validate_knowledge_card(card)
    existing = knowledge.find_by_id(str(normalized["card_id"]))
    before = _public(existing) if existing else None
    changed = sorted(
        key
        for key in set(before or {}) | set(normalized)
        if (before or {}).get(key) != normalized.get(key) and not key.startswith("_")
    )
    return response(
        "kernelcat.card-operation/v1",
        "ok",
        card_id=normalized["card_id"],
        base_revision=(before or {}).get("revision_id"),
        candidate_revision=normalized["revision_id"],
        changed_fields=changed,
        is_new=before is None,
    )


def card_publish(
    card: Mapping[str, Any],
    *,
    card_root: Path,
    repo_root: Path,
    reviewed: bool = False,
    binding: ResourceBinding | None = None,
    history_verifier: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    normalized = validate_knowledge_card(card)
    if normalized.get("status") != "active":
        return response(
            "kernelcat.card-operation/v1",
            "error",
            code="PUBLISH_GATE_FAILED",
            message="published card input must already declare status=active",
        )
    if normalized.get("card_type") in _REVIEW_REQUIRED and not reviewed:
        return response(
            "kernelcat.card-operation/v1",
            "error",
            code="REVIEW_REQUIRED",
            card_id=normalized["card_id"],
            message="this card type requires explicit --reviewed",
        )
    inputs = binding.card_roots if binding else card_roots(repo_root, card_root)
    with catalog_locks([card_root, *card_owner_roots(inputs)]):
        if binding is not None:
            current = ResourceBinding.compose(repo_root)
            if (
                current.paths.card_index_path != binding.paths.card_index_path
                or set(current.card_roots) != set(binding.card_roots)
            ):
                return response(
                    "kernelcat.card-operation/v1", "error",
                    code="RESOURCE_CONFIGURATION_CONFLICT",
                    message="publication owners changed; rebuild before a new publication",
                )
        if normalized["card_type"] == "OperatorHistory":
            if history_verifier is None:
                raise ContractError("history publication requires a selected-byte verifier")
            history_verifier(normalized)
        return _publish_locked(
            normalized, card_root=card_root, repo_root=repo_root, binding=binding
        )


def _publish_locked(
    normalized: dict[str, Any],
    *,
    card_root: Path,
    repo_root: Path,
    binding: ResourceBinding | None = None,
) -> dict[str, Any]:
    try:
        existing = existing_card(
            binding.card_roots if binding else card_roots(repo_root, card_root),
            normalized,
        )
    except ContractError as exc:
        return response(
            "kernelcat.card-operation/v1",
            "error",
            code="IDENTITY_CONFLICT",
            message=str(exc),
        )
    if existing:
        if str(existing.get("revision_id")) == str(normalized["revision_id"]):
            return response(
                "kernelcat.card-operation/v1",
                "duplicate",
                card_id=normalized["card_id"],
                revision_id=normalized["revision_id"],
            )
        return response(
            "kernelcat.card-operation/v1",
            "error",
            code="ACTIVE_REVISION_CONFLICT",
            card_id=normalized["card_id"],
            active_revision=existing.get("revision_id"),
            candidate_revision=normalized["revision_id"],
            message="use an explicit supersede workflow; active cards are not overwritten",
        )
    name = (
        hashlib.sha256(
            f"{normalized['card_id']}:{normalized['revision_id']}".encode()
        ).hexdigest()[:32]
        + ".json"
    )
    target = card_root / name
    install(target, (canonical_json(normalized) + "\n").encode("utf-8"), mode=0o666)
    return response(
        "kernelcat.card-operation/v1",
        "published",
        card_id=normalized["card_id"],
        revision_id=normalized["revision_id"],
        path=target.relative_to(repo_root).as_posix(),
    )
