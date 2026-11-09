"""Conservative migration from legacy trajectory files into the ledger."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .git_paths import head_tree_spec
from .models import ContractError, EvidenceKind, EvidenceRecord, EvidenceStatus, OperatorId, content_id
from .store import ContentAddressedStore, Ledger


@dataclass(frozen=True)
class MigrationIssue:
    line: int
    severity: str
    message: str


@dataclass(frozen=True)
class MigrationReport:
    source: str
    source_ref: str
    rows_seen: int
    rows_imported: int
    rows_skipped: int
    official_rows: int
    legacy_derived_rows: int
    issues: tuple[MigrationIssue, ...] = field(default_factory=tuple)

    @property
    def report_id(self) -> str:
        return content_id("migration", self)

    def compact(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "source": self.source,
            "source_ref": self.source_ref,
            "rows_seen": self.rows_seen,
            "rows_imported": self.rows_imported,
            "rows_skipped": self.rows_skipped,
            "official_rows": self.official_rows,
            "legacy_derived_rows": self.legacy_derived_rows,
            "issues": [issue.__dict__ for issue in self.issues],
        }


def _operator(row: Mapping[str, Any]) -> OperatorId:
    value = row.get("operator") or row.get("op") or row.get("operator_name") or "unknown_operator"
    return OperatorId.parse(str(value))


def _is_verifiable_official(row: Mapping[str, Any]) -> bool:
    required = ("submission_id", "job_id", "before_snapshot_id", "after_snapshot_id")
    return all(isinstance(row.get(key), str) and row[key].strip() for key in required)


def classify_legacy_row(
    row: Mapping[str, Any],
    *,
    source_ref: str,
    line: int,
    raw_sha256: str | None = None,
) -> EvidenceRecord:
    """Map one row without upgrading narrative labels into verified facts."""
    official = _is_verifiable_official(row)
    action = str(row.get("action") or row.get("event") or row.get("status") or "legacy").lower()
    if official and "hidden" in action:
        kind = EvidenceKind.OFFICIAL_HIDDEN
        status = EvidenceStatus.FAIL if row.get("hard_failure") is True else EvidenceStatus.PASS
    elif official:
        kind = EvidenceKind.OFFICIAL_STANDARD
        status = EvidenceStatus.FAIL if row.get("hard_failure") is True else EvidenceStatus.PASS
    else:
        kind = EvidenceKind.LEGACY_DERIVED
        status = EvidenceStatus.UNKNOWN
    source_sha = row.get("source_sha256") or row.get("candidate_sha256")
    casepack_sha = row.get("casepack_sha256")
    selected = {
        key: row.get(key)
        for key in (
            "submission_id",
            "job_id",
            "before_snapshot_id",
            "after_snapshot_id",
            "hard_failure",
            "delta",
            "score_delta",
            "tag",
        )
        if key in row
    }
    return EvidenceRecord(
        kind=kind,
        status=status,
        candidate_id=row.get("candidate_id") if str(row.get("candidate_id", "")).startswith("candidate_") else None,
        experiment_id=row.get("experiment_id") if str(row.get("experiment_id", "")).startswith("experiment_") else None,
        operator=_operator(row),
        source_tree_sha256=source_sha if isinstance(source_sha, str) and len(source_sha) == 64 else None,
        casepack_sha256=casepack_sha if isinstance(casepack_sha, str) and len(casepack_sha) == 64 else None,
        payload={
            "legacy_line": line,
            "legacy_action": action,
            "raw_line_sha256": raw_sha256,
            "selected_fields": selected,
            "classification": "verified_official" if official else "legacy_derived",
            "land_is_not_score_proof": action == "land" and not official,
        },
        artifact_refs=(source_ref,),
        provenance={"migration": "trajectory_jsonl", "line": str(line)},
    )


def migrate_trajectory(
    path: str | Path,
    *,
    cas: ContentAddressedStore,
    ledger: Ledger,
    apply: bool = False,
) -> MigrationReport:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    source_artifact = cas.put_file(source, media_type="application/x-ndjson")
    ledger.record_artifact(source_artifact)
    rows_seen = rows_imported = official_rows = legacy_rows = 0
    issues: list[MigrationIssue] = []
    for line_number, raw in enumerate(source.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not raw.strip():
            continue
        rows_seen += 1
        try:
            row = json.loads(raw)
            if not isinstance(row, Mapping):
                raise ValueError("row is not an object")
            record = classify_legacy_row(
                row,
                source_ref=source_artifact.uri,
                line=line_number,
                raw_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            )
            if record.kind is EvidenceKind.LEGACY_DERIVED:
                legacy_rows += 1
            else:
                official_rows += 1
            if apply:
                ledger.record_entity(record.evidence_id, "evidence", record)
                aggregate = record.candidate_id or f"legacy:{source_artifact.sha256}"
                ledger.append_event(aggregate, "evidence_imported", record)
            rows_imported += 1
        except (ValueError, TypeError, ContractError, json.JSONDecodeError) as exc:
            issues.append(MigrationIssue(line_number, "error", str(exc)))
    return MigrationReport(
        source=str(source),
        source_ref=source_artifact.uri,
        rows_seen=rows_seen,
        rows_imported=rows_imported,
        rows_skipped=rows_seen - rows_imported,
        official_rows=official_rows,
        legacy_derived_rows=legacy_rows,
        issues=tuple(issues),
    )


@dataclass(frozen=True)
class BackupVerification:
    manifest_path: str
    source_commit: str
    source_path: str
    backup_path: str
    expected_tree_sha: str
    source_tree_sha: str
    backup_tree_sha: str
    complete: bool

    def compact(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo_root), *args),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def verify_backup_manifest(repo_root: str | Path, manifest_path: str | Path) -> BackupVerification:
    root = Path(repo_root).expanduser().resolve()
    manifest_file = Path(manifest_path)
    if not manifest_file.is_absolute():
        manifest_file = root / manifest_file
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    source_commit = manifest["source_commit"]
    source_path = manifest["source_path"]
    backup_path = manifest["backup_path"]
    expected = manifest["source_tree_sha"]
    source_tree = _git(root, "rev-parse", f"{source_commit}:{source_path}")
    backup_tree = _git(root, "rev-parse", head_tree_spec(root, backup_path))
    return BackupVerification(
        manifest_path=str(manifest_file),
        source_commit=source_commit,
        source_path=source_path,
        backup_path=backup_path,
        expected_tree_sha=expected,
        source_tree_sha=source_tree,
        backup_tree_sha=backup_tree,
        complete=source_tree == backup_tree == expected,
    )


def compare_shadow_views(legacy: Mapping[str, Any], control_plane: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "status",
        "operator",
        "candidate_sha256",
        "casepack_sha256",
        "correctness",
        "aggregate_delta",
        "speedup_lcb95",
        "submission_id",
        "job_id",
    )
    diffs = {
        field: {"legacy": legacy.get(field), "control_plane": control_plane.get(field)}
        for field in fields
        if legacy.get(field) != control_plane.get(field)
    }
    return {"equivalent": not diffs, "diffs": diffs, "compared_fields": list(fields)}
