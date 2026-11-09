"""Map data identities to Wiki's public store; never publish active cards."""
from __future__ import annotations

from pathlib import Path

from kernelcat_executor.contracts.models import Json, canonical, digest

from kernelcat_ingestion.plans import Asset, Element, Snapshot, SourceSpec
from kernelcat_ingestion.ports import EvidenceStore
from kernelcat_ingestion.snapshots import DataError, hex_digest, sha256


class WikiEvidence:
    def __init__(self, root: Path, *, store: EvidenceStore) -> None:
        self.root = root.resolve()
        self.store = store

    @property
    def identity(self) -> str:
        return str(self.root)

    def source(self, source_id: str) -> Json:
        try:
            return dict(self.store.source(source_id))
        except DataError as exc:
            raise DataError("Wiki source unavailable or corrupt") from exc

    def capture(self, spec: SourceSpec, asset: Asset, content: bytes, origin: str) -> Snapshot:
        hashed = sha256(content)
        if asset.sha256 is not None and asset.sha256 != hashed:
            raise DataError("acquired content does not match its reviewed digest")
        source_id = "src:data:" + digest({"origin": origin, "revision": spec.revision,
                                           "digest": hashed})
        ref = "cas://sha256/" + hex_digest(hashed)
        identity = {"schema_version": "kernelcat.source/v1", "source_id": source_id,
                    "source_class": spec.source_class, "authority_scopes": ["source_material"],
                    "locator": origin, "version": spec.revision, "digest": hashed,
                    "artifact_ref": ref, "status": "active",
                    "fixture": spec.fixture, "synthetic": spec.synthetic}
        try:
            self.store.capture(identity, content)
        except DataError as exc:
            raise DataError(str(exc)) from exc
        return Snapshot(source_id=source_id, source_key=spec.key, name=asset.path,
                        revision=spec.revision, artifact_ref=ref, artifact_digest=hashed,
                        role=asset.role, grant_digest=digest(spec), size=len(content))

    def read(self, snapshot: Snapshot) -> bytes:
        source = self.source(snapshot.source_id)
        if (source["status"] != "active" or source["digest"] != snapshot.artifact_digest
                or source.get("artifact_ref") != snapshot.artifact_ref):
            raise DataError("inactive source or snapshot identity mismatch")
        try:
            content = self.store.read_source(snapshot.source_id)
        except DataError as exc:
            raise DataError("Wiki source verification failed") from exc
        if (not isinstance(content, bytes) or len(content) != snapshot.size
                or sha256(content) != snapshot.artifact_digest):
            raise DataError("source size or digest differs from snapshot")
        return content

    def register_element(self, snapshot: Snapshot, element: Element) -> None:
        if element.source_id != snapshot.source_id:
            raise DataError("element belongs to a different source")
        source = self.source(snapshot.source_id)
        record = {"schema_version": "kernelcat.evidence/v1", "evidence_id": element.evidence_id,
                  "source_id": snapshot.source_id, "artifact_digest": snapshot.artifact_digest,
                  "selector": element.selector, "observed_at": source["retrieved_at"],
                  "producer": "kernelcat-data/native-lossless-v1", "integrity": "verified",
                  "interpretation_digest": digest(element)}
        if "parser_identity" in element.context:
            record["producer"] = "kernelcat-data/native-lossless-v2"
            record["parser_identity"] = element.context["parser_identity"]
        try:
            self.store.register_evidence(record)
        except DataError as exc:
            raise DataError("Wiki evidence registration failed") from exc

    def check(self, snapshot: Snapshot, elements: tuple[Element, ...]) -> None:
        """Verify source bytes once, then check each parsed evidence binding."""
        self.read(snapshot)
        for element in elements:
            self._check_element(snapshot, element)

    def _check_element(self, snapshot: Snapshot, element: Element) -> None:
        try:
            row = self.store.evidence(element.evidence_id)
        except DataError as exc:
            raise DataError("Wiki evidence unavailable or corrupt") from exc
        if (element.source_id != snapshot.source_id
                or row is None or row["integrity"] != "verified"
                or row["source_id"] != snapshot.source_id
                or row["artifact_digest"] != snapshot.artifact_digest
                or canonical(row["selector"]) != canonical(element.selector)
                or row.get("interpretation_digest") != digest(element)):
            raise DataError("missing, corrupt or mismatched Wiki evidence")
