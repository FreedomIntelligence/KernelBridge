"""Finite verified history reads through the existing Knowledge facade."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kernelcat_knowledge.cards.history import (
    RULE_ID,
    RULE_VERSION,
    interpret,
    rule_digest,
    strict_document,
    verify_raw,
)
from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    validate_knowledge_card,
)
from kernelcat_knowledge.contracts.canonical import sha256_digest

if TYPE_CHECKING:
    from kernelcat_knowledge.api.application import WikiApplication


class HistoryVerifier:
    """Call-local selected-byte cache, bounded to the supplied card (never retained)."""

    def __init__(self, app: WikiApplication) -> None:
        self.app = app
        self.selected: dict[str, tuple[dict[str, Any], dict[str, Any], Any]] = {}
        self.pins: dict[str, str] = {}
        self.manifest_digest: str | None = None

    def selection(self, evidence_id: str) -> tuple[dict[str, Any], dict[str, Any], Any]:
        if evidence_id in self.selected:
            return self.selected[evidence_id]
        if len(self.selected) >= 4096:
            raise ContractError("history exceeds 4096 provenance selections")
        shown = self.app.evidence_show(evidence_id)
        if shown["status"] != "ok":
            raise ContractError("history evidence unavailable: " + evidence_id)
        evidence = shown["evidence"]
        source_result = self.app.source_show(evidence["source_id"])
        if source_result["status"] != "ok":
            raise ContractError("history source unavailable: " + evidence_id)
        source = source_result["source"]
        document = strict_document(self.selected_text(evidence_id, evidence["selector"]))
        self.selected[evidence_id] = (evidence, source, document)
        return evidence, source, document

    def selected_text(self, evidence_id: str, selector: dict[str, Any]) -> str:
        result = self.app.raw_read(evidence_id=evidence_id)
        parts = []
        size = 0
        while True:
            if result["status"] != "ok":
                raise ContractError("history source read failed: " + evidence_id)
            selected = result["result"]
            text = selected["content"]
            if len(text.split("\n")) != selected["end"] - selected["start"] + 1:
                raise ContractError("history source line exceeds read byte bound")
            parts.append(text)
            size += len(text.encode()) + 1
            end = selected["total_lines"] if selector["kind"] == "document" else selector["end"]
            if size > 1048576 or end > 4096:
                raise ContractError("history provenance exceeds 1 MiB or 4096 physical lines")
            if selected["end"] == end:
                if selected["truncated"]:
                    raise ContractError("history source selection is truncated")
                return "\n".join(parts)
            result = self.app.raw_read(evidence_id=evidence_id, selector_override={
                "line_start": selected["end"] + 1,
                "line_end": min(selected["end"] + self.app.binding.paths.raw_max_lines, end),
            })

    def field(self, value: dict[str, Any], *, campaign: bool) -> None:
        address = value["address"]
        evidence, _source, document = self.selection(address["evidence_id"])
        selector = evidence["selector"]
        if address["format"] == "json":
            if selector["kind"] != "document" or set(selector) & {"start", "end", "line_start", "line_end"}:
                raise ContractError("JSON history requires a complete document selection")
        elif (selector["kind"] != "lines" or selector.get("start") != address["line"]
              or selector.get("end") != address["line"]):
            raise ContractError("JSONL history address leaves its exact physical line selection")
        parser_identity = evidence.get("parser_identity")
        if (parser_identity is not None
                and parser_identity["recipe"] in {"campaign-history/v1", "campaign-history/v2"}
                and not campaign):
            raise ContractError("campaign evidence requires the bound campaign interpretation rule")
        verify_raw(value, document)
        if campaign and canonical_json(interpret(address, document)) != canonical_json(value):
            raise ContractError("history interpretation differs from campaign rules")

    def verify(self, card: dict[str, Any], claims: list[dict[str, Any]] | None = None) -> None:
        if len(card["claims"]) > 100000 or len(card["evidence_refs"]) > 4096:
            raise ContractError("history exceeds 100000 claims or 4096 provenance references")
        derivation = next(claim["object"] for claim in card["claims"]
                          if claim["object"]["kind"] == "derivation")
        _evidence, source, manifest = self.selection(derivation["manifest_evidence_id"])
        if _evidence["selector"]["kind"] != "document":
            raise ContractError("history manifest requires a complete document selection")
        if source["digest"] != derivation["input_manifest_digest"]:
            raise ContractError("history manifest digest differs from source bytes")
        if derivation["rule_id"] == RULE_ID:
            self.pins = {row["path"]: row["sha256"] for row in manifest["files"]}
            self.manifest_digest = source["digest"]
            if derivation["rule_version"] != RULE_VERSION + ":" + rule_digest():
                raise ContractError("history campaign rule implementation changed")
            parser_ids = set()
            for evidence_id in card["evidence_refs"]:
                if evidence_id == derivation["manifest_evidence_id"]:
                    continue
                evidence = self.app.evidence_show(evidence_id)
                if evidence["status"] != "ok" or "parser_identity" not in evidence["evidence"]:
                    raise ContractError("history parser identity is unavailable")
                parser_ids.add(evidence["evidence"]["parser_identity"]["digest"])
            if sha256_digest(sorted(parser_ids)) != derivation["parser_identity_digest"]:
                raise ContractError("history parser derivation differs from evidence")
        for claim in card["claims"] if claims is None else claims:
            if claim["object"]["kind"] == "field":
                self.field(claim["object"], campaign=derivation["rule_id"] == RULE_ID)
                if derivation["rule_id"] == RULE_ID:
                    self.campaign_origin(claim, card["applicability"]["operator"][0])

    def campaign_origin(self, claim: dict[str, Any], operator: str) -> None:
        evidence, source, _document = self.selection(claim["object"]["address"]["evidence_id"])
        identity = evidence["parser_identity"]
        descriptor = identity["descriptor"]
        if sha256_digest(descriptor)[7:] != identity["digest"]:
            raise ContractError("campaign parser descriptor digest mismatch")
        environment = strict_document(descriptor["environment"])
        if environment["manifest"] != self.manifest_digest:
            raise ContractError("history parser was bound to a different manifest")
        name = descriptor["name"]
        if not source["locator"].endswith("/" + name):
            raise ContractError("history source origin differs from its parsed identity")
        origin = name.split("/")[0]
        canonical_operator = {"conv2d": "conv_2d"}.get(origin, origin)
        if canonical_operator != operator or name != claim["predicate"]:
            raise ContractError("history field is attributed to a different operator/source")
        if self.pins.get(name) != source["digest"]:
            raise ContractError("history source is not pinned in its derivation manifest")
        if descriptor["artifact_digest"] != source["digest"]:
            raise ContractError("history parser input differs from source bytes")


def verify_history(app: WikiApplication, card: dict[str, Any]) -> None:
    normalized = validate_knowledge_card(card)
    if normalized["card_type"] == "OperatorHistory":
        HistoryVerifier(app).verify(normalized)


def read_history(app: WikiApplication, card_id: str, *, offset: int = 0,
                 limit: int = 50) -> dict[str, Any]:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ContractError("history claim page requires offset >= 0 and limit 1..100")
    shown = app.show(card_id, view="audit")
    if shown["status"] != "ok":
        raise ContractError("history card not found: " + card_id)
    payload = dict(shown["object"])
    # The existing read index adds an empty legacy decision presentation field.
    # It is not card authority; nonempty decisions still fail history validation.
    if payload.get("decision") == {}:
        payload.pop("decision")
    card = validate_knowledge_card(payload)
    if card["card_type"] != "OperatorHistory":
        raise ContractError("requested card is not OperatorHistory")
    claims = card["claims"][offset:offset + limit]
    HistoryVerifier(app).verify(card, claims)
    result = {"status": "ok", "card_id": card["card_id"], "revision_id": card["revision_id"],
              "claims": claims, "offset": offset, "total_claims": len(card["claims"]),
              "next_offset": offset + len(claims) if offset + len(claims) < len(card["claims"]) else None,
              "qualification": {"current_parent": "unknown", "current_performance": "unqualified",
                                "action_authority": False}}
    if len(canonical_json(result).encode()) > 1048576:
        raise ContractError("history page exceeds 1 MiB; request fewer claims")
    return result
