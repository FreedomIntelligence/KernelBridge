from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

REPRODUCIBILITY_LEVELS: tuple[str, ...] = (
    "concept",
    "pseudocode",
    "api-valid",
    "compile-tested",
    "run-tested",
    "benchmarked",
)
REPRODUCIBILITY_ORDER = {
    name: index for index, name in enumerate(REPRODUCIBILITY_LEVELS)
}

EVIDENCE_KINDS: tuple[str, ...] = (
    "official-doc",
    "upstream-code",
    "upstream-pr",
    "benchmark-record",
    "test-record",
    "community-note",
    "inference",
)

VERIFICATION_STATUSES: tuple[str, ...] = (
    "not-run",
    "pass",
    "fail",
    "partial",
    "unknown",
)


def as_list(value: Any) -> list[Any]:
    """Normalize an optional scalar/list value without mutating the caller's data."""
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def compact_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    """Drop empty optional fields while preserving explicit zero/False values."""
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }


@dataclass(frozen=True)
class EvidenceRef:
    source: str
    locator: str
    kind: str
    claim: str = ""
    source_id: str = ""
    commit: str = ""
    confidence: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EvidenceRef:
        return cls(
            source=str(data.get("source") or ""),
            locator=str(data.get("locator") or ""),
            kind=str(data.get("kind") or ""),
            claim=str(data.get("claim") or ""),
            source_id=str(data.get("source_id") or ""),
            commit=str(data.get("commit") or ""),
            confidence=str(data.get("confidence") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return compact_dict(
            {
                "source": self.source,
                "source_id": self.source_id,
                "locator": self.locator,
                "kind": self.kind,
                "claim": self.claim,
                "commit": self.commit,
                "confidence": self.confidence,
            }
        )


@dataclass(frozen=True)
class ApplicabilityScope:
    soc: tuple[str, ...] = ()
    architecture: tuple[str, ...] = ()
    cann: tuple[str, ...] = ()
    compiler: tuple[str, ...] = ()
    framework: tuple[str, ...] = ()
    dtype: tuple[str, ...] = ()
    layout: tuple[str, ...] = ()
    shape_predicates: tuple[str, ...] = ()
    use_when: tuple[str, ...] = ()
    reject_when: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ApplicabilityScope:
        data = data or {}
        return cls(
            soc=tuple(str(x) for x in as_list(data.get("soc"))),
            architecture=tuple(str(x) for x in as_list(data.get("architecture"))),
            cann=tuple(str(x) for x in as_list(data.get("cann"))),
            compiler=tuple(str(x) for x in as_list(data.get("compiler"))),
            framework=tuple(str(x) for x in as_list(data.get("framework"))),
            dtype=tuple(str(x) for x in as_list(data.get("dtype"))),
            layout=tuple(str(x) for x in as_list(data.get("layout"))),
            shape_predicates=tuple(
                str(x) for x in as_list(data.get("shape_predicates"))
            ),
            use_when=tuple(str(x) for x in as_list(data.get("use_when"))),
            reject_when=tuple(str(x) for x in as_list(data.get("reject_when"))),
        )

    @property
    def is_scoped(self) -> bool:
        return bool(
            self.soc
            or self.architecture
            or self.cann
            or self.compiler
            or self.framework
        )

    def to_dict(self) -> dict[str, Any]:
        return compact_dict(
            {
                "soc": list(self.soc),
                "architecture": list(self.architecture),
                "cann": list(self.cann),
                "compiler": list(self.compiler),
                "framework": list(self.framework),
                "dtype": list(self.dtype),
                "layout": list(self.layout),
                "shape_predicates": list(self.shape_predicates),
                "use_when": list(self.use_when),
                "reject_when": list(self.reject_when),
            }
        )


@dataclass
class TrustMetadata:
    contract: str = ""
    reproducibility: str = "concept"
    applicability: ApplicabilityScope = field(default_factory=ApplicabilityScope)
    evidence: list[EvidenceRef] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    performance_claims: list[dict[str, Any]] = field(default_factory=list)
    version_sensitive: list[str] = field(default_factory=list)
    artifact_dir: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TrustMetadata:
        evidence = []
        for item in as_list(data.get("evidence")):
            if isinstance(item, Mapping):
                evidence.append(EvidenceRef.from_mapping(item))
        return cls(
            contract=str(data.get("trust_contract") or ""),
            reproducibility=str(data.get("reproducibility") or "concept"),
            applicability=ApplicabilityScope.from_mapping(
                data.get("applicability") if isinstance(data.get("applicability"), Mapping) else {}
            ),
            evidence=evidence,
            verification=dict(data.get("verification") or {})
            if isinstance(data.get("verification"), Mapping)
            else {},
            performance_claims=[
                dict(item)
                for item in as_list(data.get("performance_claims"))
                if isinstance(item, Mapping)
            ],
            version_sensitive=[
                str(item.get("id") if isinstance(item, Mapping) else item)
                for item in as_list(data.get("version_sensitive"))
                if item not in (None, "")
            ],
            artifact_dir=str(data.get("artifact_dir") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return compact_dict(
            {
                "trust_contract": self.contract,
                "reproducibility": self.reproducibility,
                "applicability": self.applicability.to_dict(),
                "evidence": [item.to_dict() for item in self.evidence],
                "verification": self.verification,
                "performance_claims": self.performance_claims,
                "version_sensitive": self.version_sensitive,
                "artifact_dir": self.artifact_dir,
            }
        )
