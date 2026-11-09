"""Rebuildable search read-model operations; no source/publication authority."""

from __future__ import annotations

from typing import Any

from kernelcat_knowledge.cards.active import load_cards
from kernelcat_knowledge.contracts import sha256_digest
from kernelcat_knowledge.ports import KnowledgeRepository, ReadModel


def index_rebuild(
    *, knowledge: KnowledgeRepository, read_model: ReadModel
) -> dict[str, Any]:
    knowledge.load_all()
    cards, invalid = load_cards(knowledge)
    result = read_model.rebuild(
        cards,
        active_index_revision=knowledge.active_index_revision,
    )
    result["invalid_card_count"] = len(invalid)
    if "digest" not in result:
        result["digest"] = sha256_digest(result)
    return result


def index_status(
    *, knowledge: KnowledgeRepository, read_model: ReadModel
) -> dict[str, Any]:
    knowledge.load_all()
    return read_model.status(expected_revision=knowledge.active_index_revision)
