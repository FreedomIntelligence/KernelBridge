"""Transport-neutral KnowledgeQuery -> ContextProjection orchestration."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts import (
    validate_context_projection,
    validate_knowledge_query,
)
from kernelcat_knowledge.ports import CardReadRepository
from kernelcat_knowledge.query.projection import (
    build_context_projection,
    estimate_tokens,
)


def search(request: Mapping[str, Any], *, repository: CardReadRepository) -> dict[str, Any]:
    """Validate one decision query and build a deterministic bounded projection."""
    query = validate_knowledge_query(request)
    cards, invalid = repository.cards_for_projection()
    projection = build_context_projection(
        query,
        cards,
        active_index_revision=repository.active_index_revision,
    )
    if invalid:
        message = f"{len(invalid)} 个 V1 card 未通过确定性合同，已从默认索引隔离。"
        projection["uncertainties"].append(message)
        measured = estimate_tokens(projection)
        if measured <= int(query["token_budget"]):
            projection["token_count"] = measured
        else:
            projection["uncertainties"].pop()
    return validate_context_projection(projection)
