"""Read active card revisions without changing their authority files."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from kernelcat_knowledge.cards.legacy import adapt_legacy_card
from kernelcat_knowledge.contracts import ContractError, validate_knowledge_card
from kernelcat_knowledge.ports import KnowledgeRepository


def load_cards(
    knowledge: KnowledgeRepository,
) -> tuple[list[dict[str, Any]], list[str]]:
    return validate_cards(knowledge.load_tier("dynamic"))


def validate_cards(
    entries: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply the original per-card admission/isolation rule to a fixed input."""
    cards: list[dict[str, Any]] = []
    invalid: list[str] = []
    for entry in entries:
        if entry.get("schema_version") == "kernelcat.knowledge-card/v1":
            try:
                cards.append(validate_knowledge_card(entry))
            except ContractError as exc:
                invalid.append(f"{entry.get('card_id') or entry.get('ID')}: {exc}")
            continue
        if entry.get("type") == "operator_card" or entry.get("card_id"):
            cards.append(adapt_legacy_card(entry))
    return cards, invalid
