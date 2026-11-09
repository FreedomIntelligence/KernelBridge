"""Source-attributed campaign claims and strict field interpretation.

Leaves retain their path/physical line, including repeated trial IDs. No field
establishes a currently adopted parent, measured activity or current performance.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    validate_knowledge_card,
)
from kernelcat_knowledge.contracts.canonical import sha256_digest
from kernelcat_knowledge.generated.operator_history import (
    HistoryDerivation,
    HistoryField,
    HistoryFieldAddress,
    HistoryRawValue,
)

RULE_ID = "campaign-history-fields"
RULE_VERSION = "1"


def rule_digest() -> str:
    return "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def strict_document(text: str) -> Any:
    if len(text.encode()) > 1048576:
        raise ContractError("history selection exceeds 1 MiB")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContractError("duplicate JSON key in history selection")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs)
        canonical_json(value)
        return value
    except (ValueError, RecursionError) as error:
        raise ContractError("invalid finite JSON history selection") from error


def resolve_field(document: Any, pointer: str) -> HistoryRawValue:
    if len(pointer) > 4096 or not re.fullmatch(r"(?:/(?:[^~]|~[01])*)?", pointer):
        raise ContractError("invalid history JSON pointer")
    if pointer == "":
        return {"present": True, "value": document}
    tokens = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    value = document
    for index, token in enumerate(tokens):
        if isinstance(value, dict):
            if token not in value:
                if index == len(tokens) - 1:
                    return {"present": False}
                raise ContractError("history pointer traverses an absent field")
            value = value[token]
        elif isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token) or int(token) >= len(value):
                raise ContractError("invalid history array index")
            value = value[int(token)]
        else:
            raise ContractError("history pointer traverses a scalar")
    return {"present": True, "value": value}


def leaves(value: Any, pointer: str = "", depth: int = 0) -> Iterator[str]:
    if depth > 32:
        raise ContractError("history exceeds depth 32")
    if isinstance(value, dict) and value:
        for key in sorted(value):
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from leaves(value[key], pointer + "/" + escaped, depth + 1)
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from leaves(item, pointer + "/" + str(index), depth + 1)
    else:
        yield pointer


def interpret(address: HistoryFieldAddress, document: Any) -> HistoryField:
    raw = resolve_field(document, address["pointer"])
    result: HistoryField = {"kind": "field", "address": address, "raw": raw,
                            "state": "recorded", "value": raw.get("value")}
    if not raw["present"]:
        result["state"] = "unknown"
        result["reason"] = "Field is absent in the original record."
    elif address["pointer"].endswith("/recent_30d") and document.get("seed") is True:
        result["state"] = "seeded"
        result["value"] = None
        result["reason"] = "Seeded strategy counter; measured activity is unknown."
    elif address["pointer"] == "/parent/sha256" and raw["value"] in (None, ""):
        result["state"] = "unknown"
        result["value"] = None
        result["reason"] = "Original parent digest is empty or null; verified parent is unknown."
    return result


def field_claims(operator: str, origin: str, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims = []
    for element in elements:
        document = strict_document(element["text"])
        paths = list(leaves(document))
        selector = element["selector"]
        if selector["kind"] == "lines":
            paths.extend("/" + key for key in ("trial_id", "parent", "platform", "date")
                         if key not in document)
        for pointer in sorted(paths):
            address: HistoryFieldAddress = {"evidence_id": element["evidence_id"],
                                            "format": "json", "pointer": pointer}
            if selector["kind"] == "lines":
                address = {"evidence_id": element["evidence_id"], "format": "jsonl",
                           "line": selector["start"], "pointer": pointer}
            claims.append(make_claim(operator, origin, interpret(address, document)))
            if len(claims) > 100000:
                raise ContractError("history exceeds 100000 claims per source")
    return claims


def make_claim(operator: str, predicate: str,
               value: HistoryField | HistoryDerivation) -> dict[str, Any]:
    evidence_id = (value["address"]["evidence_id"] if value["kind"] == "field"
                   else value["manifest_evidence_id"])
    return {"claim_id": "claim:history:" + sha256_digest(value)[7:], "subject": operator,
            "predicate": predicate, "object": value, "authority_scope": "historical_record",
            "evidence_refs": [evidence_id], "confidence": "high"}


def build_card(operator: str, claims: list[dict[str, Any]], derivation: HistoryDerivation,
               batch_time: str) -> dict[str, Any]:
    complete = [*claims, make_claim(operator, "derived_from", derivation)]
    return validate_knowledge_card({
        "schema_version": "kernelcat.knowledge-card/v1", "card_type": "OperatorHistory",
        "card_id": "kc:history:operator:" + operator + ":v1",
        "title": operator + " recorded history", "summary": "Source-attributed historical records.",
        "decision_question": "What did the pinned sources record?", "claims": complete,
        "applicability": {"operator": [operator]},
        "evidence_refs": sorted({ref for claim in complete for ref in claim["evidence_refs"]}),
        "evidence_maturity": "source_derived", "fact_confidence": "high",
        "attribution_confidence": "none", "status": "active", "depends_on": [], "supersedes": [],
        "created_at": batch_time, "updated_at": batch_time,
    })


def verify_raw(field: Mapping[str, Any], document: Any) -> None:
    actual = resolve_field(document, field["address"]["pointer"])
    if canonical_json(actual) != canonical_json(field["raw"]):
        raise ContractError("history field presence/value differs from selected source bytes")
