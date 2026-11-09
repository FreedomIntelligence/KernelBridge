"""Read-only original query/selector rules over a sealed exact-member repository."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from kernelcat_knowledge.cards.eligibility import FactEligibility
from kernelcat_knowledge.contracts import ContractError, sha256_digest
from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.contracts.runtime import wire_bytes, wire_digest
from kernelcat_knowledge.evidence.source_content import (
    read_evidence_content,
    verify_source_content,
)
from kernelcat_knowledge.generated.knowledge_runtime import KnowledgeViewReference
from kernelcat_knowledge.query import catalog, pages
from kernelcat_knowledge.storage.catalog_io import read_bytes
from kernelcat_knowledge.storage.retained import MAX_ARTIFACT_BYTES, RetainedStore
from kernelcat_knowledge.storage.selection import canonical_path, dependency


class RetainedRepository:
    """No current-root handles: even cached view instances verify selected blobs."""

    def __init__(self, store: RetainedStore, records: list[dict[str, Any]]) -> None:
        self.store = store
        self.records = {
            row["dependency"]["object_id"]: row
            for row in records
            if row["dependency"]["kind"] == "card_revision"
        }
        self.revisions = {
            row["dependency"]["revision_id"]: key for key, row in self.records.items()
        }
        self.active_index_revision = sha256_digest(
            sorted(
                [key, row["dependency"]["revision_id"]]
                for key, row in self.records.items()
            )
        )
        self.load_errors: list[str] = []

    def _card(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(self.store.blob(row["digest"], row["bytes"]))
        if dependency("card_revision", payload) != row["dependency"]:
            raise ContractError(
                "retained card identity differs from its selected member"
            )
        return dict(payload)

    def load_all(self) -> list[dict[str, Any]]:
        return [self._card(row) for row in self.records.values()]

    def load_all_revisions(self) -> list[dict[str, Any]]:
        return self.load_all()

    def load_tier(self, tier: str) -> list[dict[str, Any]]:
        return self.load_all() if tier == "dynamic" else []

    def find_by_id(self, knowledge_id: str) -> dict[str, Any] | None:
        logical = self.revisions.get(knowledge_id, knowledge_id)
        row = self.records.get(logical)
        return None if row is None else self._card(row)

    def find_by_path(self, path: str | Path) -> dict[str, Any] | None:
        return next(
            (card for card in self.load_all() if card.get("content_path") == str(path)),
            None,
        )


class RetainedFiles:
    def __init__(self, store: RetainedStore, manifest: dict[str, Any]) -> None:
        self.store = store
        self.artifacts = {row["key"]: row for row in manifest["artifacts"]}
        self.directories: dict[str, list[str]] = manifest["directories"]

    def path(self, locator: str) -> Path:
        return canonical_path(locator, self.store.root)

    def _read(self, key: str) -> bytes:
        row = self.artifacts.get(key)
        if row is None:
            raise ContractError(
                "required bytes are outside the retained source closure"
            )
        return self.store.blob(row["digest"], row["bytes"])

    def read(self, path: Path) -> bytes:
        return self._read("path:" + path.relative_to(self.store.root).as_posix())

    def artifact(self, reference: str) -> bytes:
        return self._read("ref:" + reference)

    def is_directory(self, path: Path) -> bool:
        return path.relative_to(self.store.root).as_posix() in self.directories

    def members(self, path: Path) -> Iterator[Path]:
        key = path.relative_to(self.store.root).as_posix()
        if key not in self.directories:
            raise ContractError("directory is outside the retained source closure")
        for member in self.directories[key]:
            yield self.store.root / member


class _UnavailableRendering:
    def get_pages(
        self,
        ids: list[str],
        *,
        follow_sources: bool = False,
        body_only: bool = False,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        return {
            "pages": [],
            "errors": [
                {"id": key, "error": "human rendering is not retained"} for key in ids
            ],
        }


class RetainedKnowledgeView:
    """Historical read selection; it never grants current execution permission."""

    def __init__(
        self,
        store: RetainedStore,
        reference: KnowledgeViewReference,
        *,
        schema_digest: str | None = None,
    ) -> None:
        from kernelcat_knowledge.query.identity import policy_digest

        self._schema_digest = schema_digest or policy_digest()
        self._store = store
        self._reference = reference
        self._assert_active()
        raw = read_bytes(store.blob_path(reference["view_digest"]), MAX_ARTIFACT_BYTES)
        # Read the manifest through the same verified blob boundary as its members.
        self._manifest = json.loads(store.blob(reference["view_digest"], len(raw)))
        self._verify_manifest()
        self._knowledge = RetainedRepository(store, self._manifest["records"])
        self._files = RetainedFiles(store, self._manifest)
        self._records = {
            (row["dependency"]["kind"], row["dependency"]["object_id"]): row
            for row in self._manifest["records"]
        }
        self._evidence_ids = frozenset(
            row["dependency"]["object_id"]
            for row in self._manifest["records"]
            if row["dependency"]["kind"] == "evidence"
        )

    @property
    def reference(self) -> KnowledgeViewReference:
        return deepcopy(self._reference)

    def _assert_active(self) -> None:
        self._store.assert_view_active(
            self._reference["view_digest"], wire_bytes(self._reference).decode("utf-8")
        )

    def _verify_manifest(self) -> None:
        from kernelcat_knowledge.resources.binding import validate_descriptor

        manifest = self._manifest
        version = manifest.get("schema_version")
        if version == "kernelcat.retained-manifest/v1":
            if "resource_config" in manifest:
                raise ContractError(
                    "V1 retained manifest cannot carry resource configuration"
                )
        elif version == "kernelcat.retained-manifest/v2":
            validate_descriptor(manifest.get("resource_config"))
        else:
            raise ContractError("unsupported retained manifest version")
        dependencies = manifest["dependencies"]
        records = manifest["records"]
        reference = self._reference
        if (
            manifest["authority_id"] != self._store.authority
            or wire_digest(dependencies) != reference["dependency_digest"]
            or [row["dependency"] for row in records] != dependencies
            or len(dependencies) != reference["member_count"]
            or sum(row["kind"] == "card_revision" for row in dependencies)
            != reference["card_count"]
            or len(manifest["artifacts"]) != reference["artifact_count"]
            or sum(row["bytes"] for row in [*records, *manifest["artifacts"]])
            != reference["total_bytes"]
        ):
            raise ContractError(
                "retained manifest does not match its exact view reference"
            )
        for row in [*records, *manifest["artifacts"]]:
            self._store.blob(row["digest"], row["bytes"])

    def _record(self, kind: str, object_id: str) -> dict[str, Any]:
        row = self._records.get((kind, object_id))
        if row is None:
            raise ContractError(
                f"{kind} ID is outside retained membership: {object_id}"
            )
        record = json.loads(self._store.blob(row["digest"], row["bytes"]))
        if dependency(kind, record) != row["dependency"]:
            raise ContractError(
                "retained record identity differs from its selected member"
            )
        return dict(record)

    def search(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from kernelcat_knowledge.query.identity import query_binding

        self._assert_active()
        descriptor = self._manifest.get("resource_config")
        return catalog.search(
            request,
            knowledge=self._knowledge,
            read_model=None,
            eligibility=FactEligibility(
                self._store.root,
                lambda key: self._record("source", key),
                lambda key: self._record("evidence", key),
                artifact_reader=self._files.artifact,
                files=self._files,
            ),
            evidence_membership=self._evidence_ids,
            binding=query_binding(
                schema_digest=self._schema_digest,
                authority=self._store.authority,
                config_digest=sha256_digest(descriptor) if descriptor else None,
                view_digest=self._reference["view_digest"],
            ),
        )

    def show(
        self, knowledge_id: str, *, view: str = "agent", max_chars: int = 12000
    ) -> dict[str, Any]:
        self._assert_active()
        if self._knowledge.find_by_id(knowledge_id) is None:
            raise ContractError(
                f"card ID is outside retained membership: {knowledge_id}"
            )
        return pages.show(
            knowledge_id,
            knowledge=self._knowledge,
            page_reader=_UnavailableRendering(),
            view=view,
            max_chars=max_chars,
        )

    def source_show(self, source_id: str) -> dict[str, Any]:
        self._assert_active()
        source = self._record("source", source_id)
        verify_source_content(
            source,
            self._store.root,
            artifact_reader=self._files.artifact,
            files=self._files,
        )
        return response("kernelcat.source-operation/v1", "ok", source=source)

    def raw_read(
        self, *, evidence_id: str, selector_override: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self._assert_active()
        evidence = self._record("evidence", evidence_id)
        source = self._record("source", evidence["source_id"])
        result = read_evidence_content(
            evidence,
            source,
            self._store.root,
            selector_override=selector_override,
            max_lines=self._manifest.get("resource_config", {})
            .get("limits", {})
            .get("raw_max_lines", 400),
            max_bytes=self._manifest.get("resource_config", {})
            .get("limits", {})
            .get("raw_max_bytes", 131072),
            artifact_reader=self._files.artifact,
            files=self._files,
        )
        return response("kernelcat.raw-result/v1", "ok", result=result)
