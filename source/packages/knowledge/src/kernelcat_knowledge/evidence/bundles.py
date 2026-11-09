"""EvidenceBundle resolution and fail-open TrajectoryDelta ingestion."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    sha256_digest,
    validate_contract,
    validate_evidence_bundle,
    validate_source_record,
)
from kernelcat_knowledge.ports import (
    ArtifactResolver,
    RepositoryError,
    ReviewQueueRepository,
    SourceRepository,
)
from kernelcat_knowledge.sources.registration import register_source

_BUNDLE_PREFIXES = ("bundle://", "bundle+repo://", "bundle+file://")


def is_bundle_ref(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith(_BUNDLE_PREFIXES)


def _bytes_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    row = {"code": code, "message": message}
    row.update(details)
    return row


class EvidenceBundleIngestor:
    """Resolve bundle refs, validate known contracts, and quarantine degradation.

    The TrajectoryDelta is persisted independently. This service never turns a
    bundle failure into a failed delta append; it records a deterministic review
    item so the original bundle can be retried after resolver/catalog repair.
    """

    def __init__(
        self,
        *,
        resolver: ArtifactResolver,
        sources: SourceRepository,
        review_queue: ReviewQueueRepository,
    ) -> None:
        self.resolver = resolver
        self.sources = sources
        self.review_queue = review_queue

    def ingest(self, delta: Mapping[str, Any]) -> dict[str, Any]:
        refs = list(
            dict.fromkeys(
                str(ref)
                for ref in delta.get("result_refs") or []
                if is_bundle_ref(str(ref))
            )
        )
        if not refs:
            return {
                "schema_version": "kernelcat.bundle-ingest-report/v1",
                "status": "skipped",
                "bundles": [],
                "review_refs": [],
            }
        bundles: list[dict[str, Any]] = []
        review_refs: list[str] = []
        for ref in refs:
            result = self._ingest_one(ref, delta)
            bundles.append(result)
            if result.get("review_ref"):
                review_refs.append(str(result["review_ref"]))
        status = "ok" if all(item.get("status") == "ingested" for item in bundles) else "degraded"
        return {
            "schema_version": "kernelcat.bundle-ingest-report/v1",
            "status": status,
            "bundles": bundles,
            "review_refs": review_refs,
        }

    def _ingest_one(self, ref: str, delta: Mapping[str, Any]) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        normalized: dict[str, Any] | None = None
        try:
            raw = self.resolver.read_bytes(ref)
        except RepositoryError as exc:
            errors.append(_error(getattr(exc, "code", "ARTIFACT_UNAVAILABLE"), str(exc)))
            return self._degraded(ref, delta, None, errors, warnings)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(_error("BUNDLE_PARSE_FAILED", str(exc)))
            return self._degraded(ref, delta, None, errors, warnings)
        if not isinstance(payload, Mapping):
            errors.append(_error("BUNDLE_PARSE_FAILED", "EvidenceBundle must be a JSON object"))
            return self._degraded(ref, delta, None, errors, warnings)
        try:
            normalized = validate_evidence_bundle(payload)
        except ContractError as exc:
            errors.append(_error("INVALID_BUNDLE_CONTRACT", str(exc)))
            return self._degraded(ref, delta, None, errors, warnings)

        bundle_digest = str(normalized["digest"])
        if ref.startswith("bundle://sha256/"):
            expected = "sha256:" + ref.rsplit("/", 1)[1]
            if expected != bundle_digest:
                errors.append(
                    _error(
                        "BUNDLE_IDENTITY_MISMATCH",
                        f"bundle ref names {expected}, manifest names {bundle_digest}",
                    )
                )
        if errors:
            return self._degraded(ref, delta, normalized, errors, warnings)

        source_ids = {str(item["source_id"]) for item in normalized["source_records"]}
        planned_sources: list[Mapping[str, Any]] = []
        duplicates = 0
        for source in normalized["source_records"]:
            source_id = str(source["source_id"])
            try:
                existing = self.sources.read(source_id)
            except RepositoryError as exc:
                errors.append(
                    _error(getattr(exc, "code", "SOURCE_READ_FAILED"), str(exc), source_id=source_id)
                )
                continue
            if existing is None:
                planned_sources.append(source)
                continue
            if existing.get("schema_version") != "kernelcat.source/v1":
                errors.append(
                    _error(
                        "CORRUPT_EXISTING_SOURCE",
                        "$.schema_version: expected 'kernelcat.source/v1'",
                        source_id=source_id,
                    )
                )
                continue
            try:
                existing_normalized = validate_source_record(existing)
            except ContractError as exc:
                errors.append(_error("CORRUPT_EXISTING_SOURCE", str(exc), source_id=source_id))
                continue
            if canonical_json(existing_normalized) != canonical_json(source):
                errors.append(
                    _error(
                        "IDENTITY_CONFLICT",
                        "the same source_id already names different immutable contents",
                        source_id=source_id,
                    )
                )
                continue
            duplicates += 1

        for evidence in normalized["evidence_refs"]:
            source_id = str(evidence["source_id"])
            if source_id in source_ids:
                continue
            try:
                existing = self.sources.read(source_id)
            except RepositoryError as exc:
                errors.append(_error(getattr(exc, "code", "SOURCE_READ_FAILED"), str(exc), source_id=source_id))
                continue
            if existing is None:
                errors.append(
                    _error(
                        "UNRESOLVED_SOURCE",
                        f"evidence {evidence['evidence_id']} refers to missing source {source_id}",
                        evidence_id=evidence["evidence_id"],
                        source_id=source_id,
                    )
                )
                continue
            if existing.get("schema_version") != "kernelcat.source/v1":
                errors.append(
                    _error(
                        "CORRUPT_EXISTING_SOURCE",
                        "$.schema_version: expected 'kernelcat.source/v1'",
                        source_id=source_id,
                    )
                )
                continue
            try:
                validate_source_record(existing)
            except ContractError as exc:
                errors.append(
                    _error("CORRUPT_EXISTING_SOURCE", str(exc), source_id=source_id)
                )

        evidence_ids = {str(item["evidence_id"]) for item in normalized["evidence_refs"]}
        for event in normalized["attempt_events"]:
            for evidence_id in event.get("evidence_refs") or []:
                if str(evidence_id) not in evidence_ids:
                    errors.append(
                        _error(
                            "UNRESOLVED_EVIDENCE",
                            f"attempt event {event['event_id']} refers to missing evidence {evidence_id}",
                            event_id=event["event_id"],
                            evidence_id=evidence_id,
                        )
                    )

        artifact_verified = 0
        for artifact in normalized["artifact_refs"]:
            artifact_ref = str(artifact["ref"])
            required = bool(artifact.get("required", True))
            try:
                content = self.resolver.read_bytes(artifact_ref)
            except RepositoryError as exc:
                target = errors if required else warnings
                target.append(_error(getattr(exc, "code", "ARTIFACT_UNAVAILABLE"), str(exc), ref=artifact_ref))
                continue
            actual = _bytes_digest(content)
            if actual != artifact["artifact_digest"]:
                target = errors if required else warnings
                target.append(
                    _error(
                        "ARTIFACT_DIGEST_MISMATCH",
                        f"expected {artifact['artifact_digest']}, got {actual}",
                        ref=artifact_ref,
                    )
                )
                continue
            schema_version = artifact.get("schema_version")
            if schema_version:
                try:
                    artifact_payload = json.loads(content.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    target = errors if required else warnings
                    target.append(_error("ARTIFACT_PARSE_FAILED", str(exc), ref=artifact_ref))
                    continue
                if not isinstance(artifact_payload, Mapping):
                    target = errors if required else warnings
                    target.append(_error("ARTIFACT_PARSE_FAILED", "contract artifact must be an object", ref=artifact_ref))
                    continue
                if artifact_payload.get("schema_version") != schema_version:
                    target = errors if required else warnings
                    target.append(
                        _error(
                            "ARTIFACT_SCHEMA_MISMATCH",
                            f"descriptor expects {schema_version}, payload declares {artifact_payload.get('schema_version')}",
                            ref=artifact_ref,
                        )
                    )
                    continue
                try:
                    validate_contract(artifact_payload)
                except ContractError as exc:
                    target = errors if required else warnings
                    code = "UNSUPPORTED_CONTRACT" if "unsupported contract" in str(exc) else "INVALID_ARTIFACT_CONTRACT"
                    target.append(_error(code, str(exc), ref=artifact_ref, schema_version=schema_version))
                    continue
            artifact_verified += 1

        registered = 0
        if not errors:
            for source in planned_sources:
                result = register_source(source, repository=self.sources)
                if result.get("status") == "registered":
                    registered += 1
                elif result.get("status") == "duplicate":
                    duplicates += 1
                else:
                    errors.append(
                        _error(
                            str(result.get("code") or "SOURCE_REGISTER_FAILED"),
                            str(result.get("message") or "source registration failed"),
                            source_id=source.get("source_id"),
                        )
                    )

        cas_ref: str | None = None
        if not errors:
            try:
                cas_ref = self.resolver.store_contract(bundle_digest, canonical_json(normalized) + "\n")
            except RepositoryError as exc:
                errors.append(_error(getattr(exc, "code", "CAS_WRITE_FAILED"), str(exc)))

        result = {
            "ref": ref,
            "bundle_id": normalized["bundle_id"],
            "digest": bundle_digest,
            "status": "ingested" if not errors else "degraded",
            "cas_ref": cas_ref,
            "sources_registered": registered,
            "sources_duplicate": duplicates,
            "evidence_refs": len(normalized["evidence_refs"]),
            "query_receipts": len(normalized["query_receipts"]),
            "attempt_events": len(normalized["attempt_events"]),
            "artifacts_verified": artifact_verified,
            "errors": errors,
            "warnings": warnings,
        }
        if errors:
            return self._degraded(ref, delta, normalized, errors, warnings, partial=result)
        return result

    def _degraded(
        self,
        ref: str,
        delta: Mapping[str, Any],
        bundle: Mapping[str, Any] | None,
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        *,
        partial: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = {
            "trajectory_delta_id": delta.get("trajectory_delta_id"),
            "trajectory_digest": delta.get("digest"),
            "bundle_ref": ref,
            "bundle_digest": bundle.get("digest") if bundle else None,
            "errors": errors,
        }
        review_id = "br_" + sha256_digest(identity).split(":", 1)[1][:24]
        review = {
            "schema_version": "kernelcat.evidence-bundle-review/v1",
            "review_id": review_id,
            "status": "review_required",
            **identity,
            "warnings": warnings,
        }
        review_ref: str | None = None
        try:
            review_ref = self.review_queue.write_atomic(review_id, canonical_json(review) + "\n")
        except RepositoryError as exc:
            errors = [*errors, _error(getattr(exc, "code", "REVIEW_WRITE_FAILED"), str(exc))]
        result = dict(partial or {})
        result.update(
            {
                "ref": ref,
                "bundle_id": bundle.get("bundle_id") if bundle else None,
                "digest": bundle.get("digest") if bundle else None,
                "status": "degraded",
                "errors": errors,
                "warnings": warnings,
                "review_ref": review_ref,
            }
        )
        return result
