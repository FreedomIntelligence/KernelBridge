"""SourceRecord catalog semantics independent of filesystem storage."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    sha256_digest,
    validate_source_record,
)
from kernelcat_knowledge.ports import (
    CorruptRecordError,
    IntegrityError,
    RepositoryError,
    SourceContentReader,
    SourceRepository,
)
from kernelcat_knowledge.ports.errors import ApplicationError


def register_source(record: Mapping[str, Any], *, repository: SourceRepository) -> dict[str, Any]:
    if record.get("schema_version") != "kernelcat.source/v1":
        return {
            "status": "error",
            "code": "INVALID_CONTRACT",
            "message": "$.schema_version: expected 'kernelcat.source/v1'",
        }
    try:
        normalized = validate_source_record(record)
    except ContractError as exc:
        return {"status": "error", "code": "INVALID_CONTRACT", "message": str(exc)}
    source_id = str(normalized["source_id"])
    payload = canonical_json(normalized) + "\n"
    record_digest = sha256_digest(normalized)
    try:
        existing = repository.read(source_id)
    except CorruptRecordError as exc:
        return {"status": "error", "code": "CORRUPT_EXISTING_SOURCE", "message": str(exc)}
    except RepositoryError as exc:
        return {"status": "error", "code": "READ_FAILED", "message": str(exc)}
    if existing is not None:
        if existing.get("schema_version") != "kernelcat.source/v1":
            return {
                "status": "error",
                "code": "CORRUPT_EXISTING_SOURCE",
                "message": "$.schema_version: expected 'kernelcat.source/v1'",
            }
        try:
            existing_normalized = validate_source_record(existing)
        except ContractError as exc:
            return {"status": "error", "code": "CORRUPT_EXISTING_SOURCE", "message": str(exc)}
        if canonical_json(existing_normalized) == canonical_json(normalized):
            return {
                "status": "duplicate",
                "source_id": source_id,
                "digest": record_digest,
                "source_digest": normalized["digest"],
                "ref": f"source://{source_id}",
            }
        return {
            "status": "error",
            "code": "IDENTITY_CONFLICT",
            "message": "the same source_id already names different immutable contents",
            "source_id": source_id,
        }
    try:
        repository.write_atomic(source_id, payload)
    except IntegrityError as exc:
        return {
            "status": "error",
            "code": "IDENTITY_CONFLICT",
            "message": str(exc),
            "source_id": source_id,
        }
    except RepositoryError as exc:
        return {"status": "error", "code": "WRITE_FAILED", "message": str(exc)}
    return {
        "status": "registered",
        "source_id": source_id,
        "digest": record_digest,
        "source_digest": normalized["digest"],
        "ref": f"source://{source_id}",
    }


def show_source(source_id: str, *, repository: SourceRepository) -> dict[str, Any]:
    try:
        row = repository.read(source_id)
    except RepositoryError as exc:
        raise ApplicationError("SOURCE_READ_FAILED", str(exc), exit_code=6) from exc
    if row is None:
        raise ApplicationError("NOT_FOUND", "source record not found", exit_code=3)
    if row.get("schema_version") != "kernelcat.source/v1":
        raise ApplicationError(
            "SOURCE_CORRUPT",
            "$.schema_version: expected 'kernelcat.source/v1'",
            exit_code=6,
        )
    try:
        return validate_source_record(row)
    except ContractError as exc:
        raise ApplicationError("SOURCE_CORRUPT", str(exc), exit_code=6) from exc


def list_sources(
    *,
    repository: SourceRepository,
    source_class: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    try:
        rows = repository.list_records()
    except RepositoryError as exc:
        raise ApplicationError("SOURCE_READ_FAILED", str(exc), exit_code=6) from exc
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if row.get("schema_version") != "kernelcat.source/v1":
            raise ApplicationError(
                "SOURCE_CORRUPT",
                "$.schema_version: expected 'kernelcat.source/v1'",
                exit_code=6,
            )
        try:
            source = validate_source_record(row)
        except ContractError as exc:
            raise ApplicationError("SOURCE_CORRUPT", str(exc), exit_code=6) from exc
        if source_class and source.get("source_class") != source_class:
            continue
        if status and source.get("status") != status:
            continue
        normalized.append(source)
    normalized.sort(key=lambda item: str(item["source_id"]))
    return {
        "schema_version": "kernelcat.source-list/v1",
        "sources": normalized,
        "total": len(normalized),
        "filters": {"source_class": source_class, "status": status},
    }


def verify_source(
    source_id: str,
    *,
    repository: SourceRepository,
    resolver: SourceContentReader | None = None,
) -> dict[str, Any]:
    source = show_source(source_id, repository=repository)
    record_digest = sha256_digest(source)
    if resolver is None:
        return {
            "schema_version": "kernelcat.source-verification/v1",
            "source_id": source_id,
            "record_digest": record_digest,
            "source_digest": source["digest"],
            "catalog_verified": True,
            "content_verified": False,
            "status": "metadata_only",
            "reason": "No SourceContentReader was configured; source bytes were not re-read",
        }
    try:
        observation = dict(resolver.inspect_source(source))
    except RepositoryError as exc:
        raise ApplicationError("SOURCE_UNAVAILABLE", str(exc), exit_code=6) from exc
    if observation.get("status") == "error":
        raise ApplicationError(
            str(observation.get("code") or "SOURCE_INTEGRITY_FAILED"),
            str(observation.get("message") or "Source content could not be verified"),
            exit_code=6,
        )
    if observation.get("status") == "verified" and observation.get("actual_digest") != source["digest"]:
        raise ApplicationError("SOURCE_INTEGRITY_FAILED", "Source reader returned a mismatched content digest", exit_code=6)
    return {
        **observation,
        "schema_version": "kernelcat.source-verification/v1",
        "source_id": source_id,
        "record_digest": record_digest,
        "source_digest": source["digest"],
        "catalog_verified": True,
        "content_verified": observation.get("status") == "verified",
    }
