"""Typed history interpretation; original source-byte verification stays at read time."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts.canonical import canonical_json
from kernelcat_knowledge.contracts.schema import (
    SchemaError,
    validate_definition,
    wire_bytes,
)
from kernelcat_knowledge.generated.operator_history import SCHEMA

HISTORY_FORBIDDEN_FIELDS = frozenset({
    "reward_scope", "measurements", "formal", "policy_id", "mechanism_family",
    "rewrite", "source_claims", "decision", "use_when", "reject_when",
})


def _claim_evidence(claim: Mapping[str, Any], card_refs: set[str]) -> str:
    value = claim["object"]
    evidence_id = (
        value["address"]["evidence_id"] if value["kind"] == "field"
        else value["manifest_evidence_id"]
    )
    if evidence_id not in claim["evidence_refs"] or not set(claim["evidence_refs"]) <= card_refs:
        raise SchemaError("history address must belong to both claim and card evidence")
    return str(value["kind"])


def _field_value(value: Mapping[str, Any]) -> None:
    if (
        value["state"] in {"recorded", "unqualified"}
        and canonical_json(value["value"]) != canonical_json(value["raw"]["value"])
    ):
        raise SchemaError("recorded history value must equal its original raw value")
    if value["state"] != "recorded" and not value["reason"].strip():
        raise SchemaError("interpreted history requires a nonempty reason")


def validate_history_card(card: Mapping[str, Any]) -> None:
    """Validate a detached card's generated shape and cross-field identities."""
    validate_definition(card["card_id"], SCHEMA, "HistoryCardId")
    operator = card["card_id"].split(":")[3]
    if card["applicability"].get("operator") not in (operator, [operator]):
        raise SchemaError("OperatorHistory scope must name its one canonical operator")
    if card["evidence_maturity"] != "source_derived" or card["attribution_confidence"] != "none":
        raise SchemaError("OperatorHistory cannot claim execution or reward qualification")
    if HISTORY_FORBIDDEN_FIELDS.intersection(card):
        raise SchemaError("OperatorHistory cannot carry policy, measurement or reward payloads")
    kinds: list[str] = []
    card_refs = set(card["evidence_refs"])
    for claim in card["claims"]:
        value = claim["object"]
        wire_bytes(value, allow_floats=True)
        validate_definition(value, SCHEMA, "HistoryClaimObject")
        if claim["subject"] != operator or claim["authority_scope"] != "historical_record":
            raise SchemaError("history claim must name its canonical operator and historical scope")
        kind = _claim_evidence(claim, card_refs)
        if kind == "field":
            _field_value(value)
        kinds.append(kind)
    if kinds.count("derivation") != 1 or "field" not in kinds:
        raise SchemaError("OperatorHistory requires one derivation and at least one field claim")
