"""Local CAS resolver and durable review queue for EvidenceBundle ingestion."""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from kernelcat_knowledge.evidence.source_content import inspect_source_content
from kernelcat_knowledge.ports import (
    ArtifactUnavailableError,
    IntegrityError,
    RepositoryError,
)
from kernelcat_knowledge.storage.catalog_io import install, read_bytes

_REVIEW_ID = re.compile(r"^br_[A-Za-z0-9_-]{1,120}$")


def _install_immutable(target: Path, payload: str) -> None:
    """Atomically install a complete immutable file without overwriting a race."""
    try:
        install(target, payload.encode("utf-8"), mode=0o666)
    except FileExistsError as exc:
        raise IntegrityError(f"immutable object {target.name} already contains different bytes") from exc
    except (OSError, UnicodeError) as exc:
        raise RepositoryError(str(exc)) from exc


class FilesystemArtifactResolver:
    """Resolve bundle/CAS refs without allowing file reads outside the repo root."""

    def __init__(self, *, repo_root: Path, cas_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.cas_root = Path(os.path.abspath(cas_root))

    def _artifact_path(self, hex_digest: str) -> Path:
        return self.cas_root / "sha256" / hex_digest[:2] / hex_digest

    def _bundle_path(self, hex_digest: str) -> Path:
        return self.cas_root / "contracts" / "sha256" / hex_digest[:2] / hex_digest

    def _inside_repo(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise ArtifactUnavailableError(
                "file bundle path must stay inside repository root"
            ) from exc
        return resolved

    @staticmethod
    def _hex_from_ref(ref: str, prefix: str) -> str:
        hex_digest = ref[len(prefix) :]
        if len(hex_digest) != 64 or any(
            character not in "0123456789abcdef" for character in hex_digest
        ):
            raise ArtifactUnavailableError(f"invalid content-addressed ref: {ref}")
        return hex_digest

    def resolve_path(self, ref: str) -> Path:
        bundle_prefix = "bundle://sha256/"
        if ref.startswith(bundle_prefix):
            return self._bundle_path(self._hex_from_ref(ref, bundle_prefix))
        for prefix in ("cas://sha256/", "artifact://sha256/"):
            if ref.startswith(prefix):
                return self._artifact_path(self._hex_from_ref(ref, prefix))
        for prefix in ("bundle+repo://", "repo://"):
            if ref.startswith(prefix):
                relative = unquote(ref[len(prefix) :]).lstrip("/")
                return self._inside_repo(self.repo_root / relative)
        if ref.startswith("bundle+file://"):
            parsed = urlparse(ref[len("bundle+") :])
            return self._inside_repo(Path(unquote(parsed.path)))
        if ref.startswith("file://"):
            parsed = urlparse(ref)
            return self._inside_repo(Path(unquote(parsed.path)))
        raise ArtifactUnavailableError(f"unsupported artifact ref: {ref}")

    def read_bytes(self, ref: str) -> bytes:
        path = self.resolve_path(ref)
        try:
            return read_bytes(path)
        except OSError as exc:
            raise ArtifactUnavailableError(f"artifact read failed: {exc}") from exc

    def inspect_source(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        return inspect_source_content(record, self.repo_root, artifact_reader=self.read_bytes)

    def store_contract(self, digest: str, canonical_payload: str) -> str:
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise IntegrityError("contract digest must be sha256:<64 hex>")
        hex_digest = digest.split(":", 1)[1]
        if any(character not in "0123456789abcdef" for character in hex_digest):
            raise IntegrityError("contract digest must use lowercase hexadecimal")
        target = self._bundle_path(hex_digest)
        _install_immutable(target, canonical_payload)
        return f"bundle://sha256/{hex_digest}"

    @staticmethod
    def bytes_digest(content: bytes) -> str:
        return "sha256:" + hashlib.sha256(content).hexdigest()


class FilesystemReviewQueue:
    def __init__(self, review_dir: Path) -> None:
        self.review_dir = Path(review_dir)

    def write_atomic(self, review_id: str, canonical_payload: str) -> str:
        if not _REVIEW_ID.fullmatch(review_id):
            raise RepositoryError("review_id must match br_<stable-id>")
        target = self.review_dir / f"{review_id}.json"
        _install_immutable(target, canonical_payload)
        return f"bundle-review://{review_id}"
