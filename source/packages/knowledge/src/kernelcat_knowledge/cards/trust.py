from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kernelcat_knowledge.cards.trust_models import (
    EVIDENCE_KINDS,
    REPRODUCIBILITY_LEVELS,
    REPRODUCIBILITY_ORDER,
    VERIFICATION_STATUSES,
    TrustMetadata,
    as_list,
)

_FRONTMATTER_RE = re.compile(
    r"^---\s*\r?\n(?P<frontmatter>.*?)\r?\n---\s*\r?\n(?P<body>.*)$",
    re.DOTALL,
)
_TRUST_CONTRACT_BYTES_RE = re.compile(
    rb"(?m)^\s*trust_contract\s*:\s*['\"]?v1['\"]?\s*$"
)
_OPERATOR_CARD_BYTES_RE = re.compile(
    rb"(?m)^\s*type\s*:\s*['\"]?operator_card['\"]?\s*$"
)
_INTERNAL_PREFIXES = ("wiki/", "raw/", "artifacts/", "data/", "tests/")
_PERF_FIELDS = ("soc", "dtype", "shape", "metric", "value", "source_id", "locator")


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    path: str
    message: str
    field: str = ""


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)
    scanned_files: int = 0
    opted_in_files: int = 0

    def add(self, level: str, code: str, path: str | Path, message: str, field: str = "") -> None:
        self.findings.append(Finding(level, code, str(path), message, field))

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.level == "warning"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_files": self.scanned_files,
            "opted_in_files": self.opted_in_files,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [asdict(item) for item in self.findings],
        }


class TrustValidator:
    """Validate Trust Contract v1 while keeping the legacy wiki migration-safe."""

    def __init__(
        self,
        repo_root: Path,
        *,
        contract_path: Path | None = None,
        version_registry_path: Path | None = None,
        legacy_audit: bool = False,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.contract_path = (contract_path or self.repo_root / "data/trust-contract.yaml").resolve()
        self.version_registry_path = (
            version_registry_path or self.repo_root / "data/version-claims.yaml"
        ).resolve()
        self.legacy_audit = legacy_audit
        self.contract = self._load_yaml(self.contract_path, {})
        registry = self._load_yaml(self.version_registry_path, {"claims": []})
        self.version_claims = {
            str(row["id"]): row
            for row in as_list(registry.get("claims"))
            if isinstance(row, Mapping) and row.get("id")
        }
        self._page_pointers: dict[str, set[str]] = {}

    @staticmethod
    def _load_yaml(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return default if data is None else data

    @staticmethod
    def parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        if not match:
            return {}, text
        data = yaml.safe_load(match.group("frontmatter")) or {}
        return (data if isinstance(data, dict) else {}), match.group("body")

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)

    @staticmethod
    def _is_opted_in(data: Mapping[str, Any]) -> bool:
        return data.get("trust_contract") == "v1" or data.get("type") == "operator_card"

    def _utf8_is_required(self, path: Path) -> bool:
        """Trusted content is UTF-8-only; legacy pages may be skipped during migration."""
        rel = self._relative(path)
        if rel.startswith(("wiki/cards/", "artifacts/")):
            return True
        try:
            head = path.read_bytes()[:65536]
        except OSError:
            return False
        return bool(
            _TRUST_CONTRACT_BYTES_RE.search(head)
            or _OPERATOR_CARD_BYTES_RE.search(head)
        )

    def _resolve_internal(self, raw: str) -> Path | None:
        value = raw.split("#", 1)[0].strip()
        if not value.startswith(_INTERNAL_PREFIXES):
            return None
        candidate = (self.repo_root / value).resolve()
        try:
            candidate.relative_to(self.repo_root)
        except ValueError:
            return None
        return candidate

    def validate_mapping(self, data: Mapping[str, Any], *, path: str, report: ValidationReport) -> None:
        if not self._is_opted_in(data):
            if self.legacy_audit and data.get("performance_claims"):
                report.add(
                    "warning", "legacy-performance-uncontracted", path,
                    "performance_claims exist without trust_contract: v1", "trust_contract",
                )
            return

        report.opted_in_files += 1
        meta = TrustMetadata.from_mapping(data)
        if data.get("trust_contract") != "v1":
            report.add("error", "missing-contract-version", path, "declare trust_contract: v1", "trust_contract")
        for field_name in ("id", "title"):
            if not str(data.get(field_name) or "").strip():
                report.add("error", f"missing-{field_name}", path, f"trusted page requires {field_name}", field_name)
        if meta.reproducibility not in REPRODUCIBILITY_LEVELS:
            report.add(
                "error", "invalid-reproducibility", path,
                f"expected one of {', '.join(REPRODUCIBILITY_LEVELS)}", "reproducibility",
            )
        if not meta.applicability.is_scoped:
            report.add(
                "error", "unscoped-applicability", path,
                "identify at least one SoC/architecture/CANN/compiler/framework scope", "applicability",
            )
        if not meta.evidence:
            report.add("error", "missing-evidence", path, "trusted pages require evidence", "evidence")
        for index, evidence in enumerate(meta.evidence):
            field_name = f"evidence[{index}]"
            if not evidence.source:
                report.add("error", "missing-evidence-source", path, "source is required", field_name)
            if not evidence.locator:
                report.add("error", "missing-evidence-locator", path, "locator is required", field_name)
            if evidence.kind not in EVIDENCE_KINDS:
                report.add("error", "invalid-evidence-kind", path, f"unsupported kind: {evidence.kind}", field_name)
            target = self._resolve_internal(evidence.source)
            if target is not None and not target.exists():
                report.add("error", "missing-evidence-path", path, f"not found: {evidence.source}", field_name)

        self._validate_verification(meta, path, report)
        for index, claim in enumerate(meta.performance_claims):
            for required in _PERF_FIELDS:
                if claim.get(required) in (None, "", []):
                    report.add(
                        "error", "incomplete-performance-claim", path,
                        f"performance_claims[{index}] missing {required}",
                        f"performance_claims[{index}].{required}",
                    )
        self._validate_version_pointers(meta, path, report)
        self._validate_artifact(meta, path, report)

    def _validate_verification(self, meta: TrustMetadata, path: str, report: ValidationReport) -> None:
        verification = meta.verification
        order = REPRODUCIBILITY_ORDER.get(meta.reproducibility, 0)
        for name, record in verification.items():
            if not isinstance(record, Mapping):
                report.add("error", "invalid-verification-section", path, f"verification.{name} must be a mapping", f"verification.{name}")
                continue
            status = str(record.get("status") or "unknown")
            if status not in VERIFICATION_STATUSES:
                report.add("error", "invalid-verification-status", path, f"unsupported status: {status}", f"verification.{name}.status")
        if order >= REPRODUCIBILITY_ORDER["api-valid"] and not any(
            item.kind in {"official-doc", "upstream-code", "upstream-pr"} for item in meta.evidence
        ):
            report.add("error", "api-valid-without-authoritative-evidence", path, "api-valid requires authoritative evidence", "evidence")
        if order >= REPRODUCIBILITY_ORDER["compile-tested"]:
            record = verification.get("compile")
            if not isinstance(record, Mapping) or record.get("status") != "pass":
                report.add("error", "compile-proof-missing", path, "compile-tested requires compile pass", "verification.compile")
            elif not any(record.get(key) for key in ("command", "artifact", "code_sha", "log")):
                report.add("error", "compile-proof-unlocatable", path, "compile receipt needs command/artifact/SHA/log", "verification.compile")
        if order >= REPRODUCIBILITY_ORDER["run-tested"]:
            record = verification.get("correctness")
            if not isinstance(record, Mapping) or record.get("status") != "pass":
                report.add("error", "correctness-proof-missing", path, "run-tested requires correctness pass", "verification.correctness")
            elif not as_list(record.get("cases")):
                report.add("error", "correctness-cases-missing", path, "correctness receipt needs cases", "verification.correctness.cases")
        if order >= REPRODUCIBILITY_ORDER["benchmarked"]:
            raw = verification.get("benchmark")
            records = raw if isinstance(raw, list) else [raw]
            records = [row for row in records if isinstance(row, Mapping)]
            if not records:
                report.add("error", "benchmark-proof-missing", path, "benchmarked requires benchmark receipts", "verification.benchmark")
            for index, record in enumerate(records):
                for required in ("soc", "shape", "metric", "value", "iterations", "code_sha"):
                    if record.get(required) in (None, "", []):
                        report.add("error", "incomplete-benchmark-record", path, f"benchmark[{index}] missing {required}", f"verification.benchmark[{index}].{required}")

    def _validate_version_pointers(self, meta: TrustMetadata, path: str, report: ValidationReport) -> None:
        normalized = path.replace("\\", "/")
        self._page_pointers[normalized] = set(meta.version_sensitive)
        for claim_id in meta.version_sensitive:
            if claim_id not in self.version_claims:
                report.add("error", "unknown-version-claim", path, f"version claim not found: {claim_id}", "version_sensitive")

    def _validate_artifact(self, meta: TrustMetadata, path: str, report: ValidationReport) -> None:
        if not meta.artifact_dir:
            return
        artifact = self._resolve_internal(meta.artifact_dir)
        if artifact is None or not artifact.is_dir():
            report.add("error", "artifact-dir-missing", path, f"artifact_dir not found: {meta.artifact_dir}", "artifact_dir")
            return
        provenance = artifact / "PROVENANCE.yaml"
        data = self._load_yaml(provenance, {})
        for required in ("origin", "retrieved_at", "mode", "files"):
            if not isinstance(data, Mapping) or data.get(required) in (None, "", []):
                report.add("error", "incomplete-provenance", path, f"{provenance.relative_to(self.repo_root)} missing {required}", "artifact_dir")

    def _validate_registry(self, report: ValidationReport) -> None:
        for claim_id, claim in self.version_claims.items():
            for required in ("tool", "claim", "last_verified_at", "applies_to", "source_ids"):
                if claim.get(required) in (None, "", []):
                    report.add("error", "incomplete-version-claim", self._relative(self.version_registry_path), f"{claim_id} missing {required}", required)
            for raw_path in as_list(claim.get("applies_to")):
                target = (self.repo_root / str(raw_path)).resolve()
                target_rel = self._relative(target)
                if not target.exists():
                    report.add("error", "version-claim-target-missing", self._relative(self.version_registry_path), f"{claim_id} target not found: {raw_path}", "applies_to")
                    continue
                pointers = self._page_pointers.get(target_rel)
                if pointers is None and target.suffix.lower() == ".md":
                    try:
                        data, _ = self.parse_markdown(target)
                    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                        report.add(
                            "error",
                            "version-claim-target-parse-failed",
                            target_rel,
                            str(exc),
                            "applies_to",
                        )
                        continue
                    pointers = set(TrustMetadata.from_mapping(data).version_sensitive)
                if claim_id not in (pointers or set()):
                    report.add("error", "version-claim-not-bidirectional", target_rel, f"page lacks matching pointer {claim_id}", "version_sensitive")

    def validate_paths(self, paths: Iterable[Path]) -> ValidationReport:
        report = ValidationReport()
        for path in paths:
            if not path.is_file():
                report.add("error", "path-not-found", self._relative(path), "input path not found")
                continue
            report.scanned_files += 1
            rel = self._relative(path)
            try:
                if path.suffix.lower() == ".md":
                    data, _ = self.parse_markdown(path)
                    if not data and "wiki/cards/" in rel:
                        report.add("error", "card-frontmatter-missing", rel, "operator cards require YAML frontmatter")
                    self.validate_mapping(data, path=rel, report=report)
                elif path.suffix.lower() == ".jsonl":
                    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if isinstance(data, Mapping):
                            self.validate_mapping(data, path=f"{rel}:{line_no}", report=report)
            except UnicodeDecodeError as exc:
                strict_utf8 = self._utf8_is_required(path)
                report.add(
                    "error" if strict_utf8 else "warning",
                    "trusted-content-non-utf8" if strict_utf8 else "legacy-non-utf8-skipped",
                    rel,
                    f"UTF-8 decode failed at byte {exc.start}: {exc.reason}",
                )
            except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
                report.add("error", "parse-failed", rel, str(exc))
        self._validate_registry(report)
        return report

    def repository_paths(self) -> list[Path]:
        paths: list[Path] = []
        for root_name, pattern in (("wiki", "*.md"), ("wiki", "*.jsonl"), ("artifacts", "PROVENANCE.yaml")):
            root = self.repo_root / root_name
            if root.exists():
                paths.extend(sorted(root.rglob(pattern)))
        return paths

    def validate_repository(self) -> ValidationReport:
        return self.validate_paths(self.repository_paths())
