"""Operational fact eligibility; structural validity alone grants no authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kernelcat_knowledge.contracts import ContractError, validate_evidence_ref
from kernelcat_knowledge.evidence.source_content import (
    SourceContentError,
    SourceFiles,
    read_evidence_content,
)
from kernelcat_knowledge.ports import RepositoryError

RecordReader = Callable[[str], dict[str, Any] | None]


@dataclass(frozen=True)
class FactEligibility:
    """Explicit live or retained inputs; no qualification state survives a call."""

    root: Path
    source: RecordReader
    evidence: RecordReader
    artifact_reader: Callable[[str], bytes] | None = None
    files: SourceFiles | None = None

    def reason(self, card: Mapping[str, Any]) -> str | None:
        """Return the first failed obligation for a structurally validated card."""
        if card["card_type"] not in {"OfficialFact", "PlatformFact"}:
            return None
        if card["status"] != "active":
            return "FACT_INACTIVE: authoritative facts require status=active"
        if card["evidence_maturity"] not in {"source_derived", "api_valid"}:
            return (
                f"UNVERIFIABLE_MATURITY: {card['evidence_maturity']} has no supported "
                "V1 qualification receipt; source bytes cannot establish it"
            )
        if not card["claims"]:
            return "CLAIMS_REQUIRED: a fact needs scoped evidence-backed claims"
        scopes: dict[str, set[str]] = {ref: set() for ref in card["evidence_refs"]}
        for claim in card["claims"]:
            for ref in claim["evidence_refs"]:
                scopes.setdefault(ref, set()).add(claim["authority_scope"])
        if any("unknown" in values for values in scopes.values()):
            return "CLAIM_AUTHORITY_UNKNOWN: unknown scope cannot qualify a fact"
        for evidence_id, required_scopes in scopes.items():
            try:
                self._verify(evidence_id, required_scopes)
            except SourceContentError as error:
                return f"{error.code}: {evidence_id}: {error}"
            except (ContractError, RepositoryError, OSError) as error:
                return f"EVIDENCE_UNAVAILABLE: {evidence_id}: {error}"
        return None

    def _verify(self, evidence_id: str, required_scopes: set[str]) -> None:
        evidence = self.evidence(evidence_id)
        if evidence is None:
            raise SourceContentError("EVIDENCE_UNAVAILABLE", "Evidence is not registered.")
        validate_evidence_ref(evidence)
        if evidence["evidence_id"] != evidence_id:
            raise SourceContentError("EVIDENCE_IDENTITY_MISMATCH", "Evidence ID differs.")
        source = self.source(evidence["source_id"])
        if source is None:
            raise SourceContentError("SOURCE_UNAVAILABLE", "Source is not registered.")
        read_evidence_content(
            evidence, source, self.root, max_lines=1, max_bytes=1,
            artifact_reader=self.artifact_reader, files=self.files,
        )
        # Registration declares source authority. Bytes/selectors cannot promote
        # community or inference material to that authority, or prove semantics.
        if source["source_class"] != "official_source":
            raise SourceContentError(
                "SOURCE_AUTHORITY_UNSUPPORTED",
                "Facts currently require registered official_source evidence.",
            )
        if not required_scopes.issubset(source["authority_scopes"]):
            missing = sorted(required_scopes - set(source["authority_scopes"]))
            raise SourceContentError(
                "CLAIM_AUTHORITY_MISMATCH", f"Source lacks claim scopes: {missing}.",
            )
