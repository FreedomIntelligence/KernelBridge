"""Bounded host/formal checks that never overclaim device correctness."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import ContractError, OperatorId, content_id
from .store import ContentAddressedStore


@dataclass(frozen=True)
class ProofObligation:
    name: str
    statement: str
    applicability: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.statement.strip() or not self.applicability.strip():
            raise ContractError("proof obligation fields cannot be empty")


@dataclass(frozen=True)
class ProofResult:
    obligation: ProofObligation
    status: str
    method: str
    detail: str
    artifact_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        allowed = {"proved", "bounded", "host_checked", "failed", "unknown"}
        if self.status not in allowed:
            raise ContractError(f"unsupported proof status: {self.status}")
        if self.status in {"proved", "bounded", "host_checked"} and not self.artifact_refs:
            raise ContractError("positive proof result requires immutable artifacts")


@dataclass(frozen=True)
class HostProofBundle:
    operator: OperatorId
    source_tree_sha256: str
    casepack_sha256: str
    results: tuple[ProofResult, ...]
    scanner_version: str = "kernelcat-host-proof-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", OperatorId.parse(self.operator))
        if len(self.source_tree_sha256) != 64 or len(self.casepack_sha256) != 64:
            raise ContractError("host proof must bind source and casepack SHA-256")
        if not self.results:
            raise ContractError("host proof bundle cannot be empty")

    @property
    def proof_id(self) -> str:
        return content_id("proof", self)

    @property
    def passed(self) -> bool:
        return all(
            result.status in {"proved", "bounded", "host_checked"}
            for result in self.results
            if result.obligation.required
        )

    def compact(self) -> dict[str, Any]:
        return {
            "proof_id": self.proof_id,
            "operator": self.operator.value,
            "status": "pass" if self.passed else "fail",
            "source_tree_sha256": self.source_tree_sha256,
            "casepack_sha256": self.casepack_sha256,
            "obligations": [
                {
                    "name": result.obligation.name,
                    "required": result.obligation.required,
                    "status": result.status,
                    "method": result.method,
                    "detail": result.detail,
                    "artifact_refs": list(result.artifact_refs),
                }
                for result in self.results
            ],
        }


_SUSPICIOUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("case_id_branch", re.compile(r"\b(case_?id|test_?id)\b\s*(?:==|!=|in\s*\{)", re.I)),
    ("hidden_case_name", re.compile(r"\b(hidden|private)_case\b", re.I)),
    ("fixed_output_shortcut", re.compile(r"\b(memcpy|DataCopy)\b[^\n]{0,120}\b(golden|expected_output|answer)\b", re.I)),
    ("score_probe", re.compile(r"\b(score|leaderboard)\b\s*(?:==|>=|<=|>)", re.I)),
)


def source_tree_sha256(root: str | Path) -> str:
    source = Path(root).expanduser().resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    digest = hashlib.sha256()
    count = 0
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source)
        if not path.is_file() or any(part in {".git", "__pycache__"} for part in relative.parts):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        count += 1
    if count == 0:
        raise ContractError("cannot hash an empty source tree")
    return digest.hexdigest()


def scan_test_fingerprints(root: str | Path, *, cas: ContentAddressedStore) -> ProofResult:
    source = Path(root).expanduser().resolve()
    findings: list[dict[str, Any]] = []
    scanned = 0
    suffixes = {".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".sh", ".cmake", ".txt"}
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        scanned += 1
        for name, pattern in _SUSPICIOUS_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append({"rule": name, "path": path.relative_to(source).as_posix(), "line": line})
    report = {"schema_version": 1, "scanner": "kernelcat-host-proof-v1", "files_scanned": scanned, "findings": findings}
    artifact = cas.put_json(report)
    return ProofResult(
        obligation=ProofObligation(
            name="no_test_fingerprinting",
            statement="candidate branches on capabilities, not case IDs, hidden names, or expected outputs",
            applicability="all text source files in the frozen tree",
        ),
        status="failed" if findings else "host_checked",
        method="bounded static pattern scan",
        detail=f"{len(findings)} high-confidence finding(s) across {scanned} files",
        artifact_refs=(artifact.uri,),
    )


def verify_fallback_contract(
    *,
    capability_predicate: str,
    fallback_entry: str,
    discovered_symbols: Iterable[str],
    cas: ContentAddressedStore,
) -> ProofResult:
    symbols = tuple(sorted(set(discovered_symbols)))
    present = bool(capability_predicate.strip()) and fallback_entry in symbols
    artifact = cas.put_json(
        {
            "capability_predicate": capability_predicate,
            "fallback_entry": fallback_entry,
            "discovered_symbols": symbols,
            "present": present,
            "scope": "host symbol/contract check; device semantics still require correctness evidence",
        }
    )
    return ProofResult(
        obligation=ProofObligation(
            name="fallback_reachable",
            statement="unsupported-domain inputs have an explicit verified fallback entry",
            applicability=capability_predicate,
        ),
        status="host_checked" if present else "failed",
        method="symbol and contract inspection",
        detail="fallback symbol found" if present else "fallback symbol missing or predicate empty",
        artifact_refs=(artifact.uri,),
    )


def bundle_host_proof(
    *,
    operator: str,
    source_root: str | Path,
    casepack_sha256: str,
    capability_predicate: str,
    fallback_entry: str,
    discovered_symbols: Iterable[str],
    cas: ContentAddressedStore,
) -> HostProofBundle:
    return HostProofBundle(
        operator=OperatorId.parse(operator),
        source_tree_sha256=source_tree_sha256(source_root),
        casepack_sha256=casepack_sha256,
        results=(
            scan_test_fingerprints(source_root, cas=cas),
            verify_fallback_contract(
                capability_predicate=capability_predicate,
                fallback_entry=fallback_entry,
                discovered_symbols=discovered_symbols,
                cas=cas,
            ),
        ),
    )
