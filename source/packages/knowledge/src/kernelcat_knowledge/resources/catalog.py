"""Finite manifest lookup and digest-checked reads; resource contents are never executed."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_RESOURCES = 10_000


class ResourceError(ValueError):
    """An invalid manifest, missing identity or unavailable immutable resource."""


@dataclass(frozen=True)
class ResourceReference:
    resource_id: str
    version: str
    digest: str
    path: str


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ResourceError(f"resource {field} must be a non-empty, unpadded string")
    return value


def _reference(value: Any) -> ResourceReference:
    if not isinstance(value, Mapping) or set(value) != {"resource_id", "version", "digest", "selector"}:
        raise ResourceError("resource requires resource_id, version, digest and selector")
    resource_id = _text(value["resource_id"], "resource_id")
    version = _text(value["version"], "version")
    digest = _text(value["digest"], "digest")
    if _DIGEST.fullmatch(digest) is None:
        raise ResourceError(f"resource {resource_id}: digest must be a SHA-256 identity")
    selector = value["selector"]
    if not isinstance(selector, Mapping) or set(selector) != {"path"}:
        raise ResourceError(f"resource {resource_id}: only a relative path selector is supported")
    path = _text(selector["path"], "selector.path")
    parts = PurePosixPath(path)
    if parts.is_absolute() or ".." in parts.parts or "\\" in path or "\x00" in path:
        raise ResourceError(f"resource {resource_id}: selector must remain inside the resource root")
    if parts.as_posix() != path or path == ".":
        raise ResourceError(f"resource {resource_id}: selector path must be canonical")
    return ResourceReference(resource_id, version, digest, path)


class ResourceCatalog:
    """Build O(n) immutable lookup; resolve O(1), read O(resource bytes), without state writes."""

    def __init__(self, root: Path, manifest: Mapping[str, Any]) -> None:
        if not isinstance(manifest, Mapping) or set(manifest) != {"resources"}:
            raise ResourceError("resource manifest requires exactly one resources array")
        rows = manifest["resources"]
        if not isinstance(rows, list) or len(rows) > _MAX_RESOURCES:
            raise ResourceError(f"resources must be an array of at most {_MAX_RESOURCES} entries")
        references: dict[tuple[str, str], ResourceReference] = {}
        for row in rows:
            reference = _reference(row)
            key = (reference.resource_id, reference.version)
            if key in references:
                raise ResourceError(f"duplicate resource identity: {reference.resource_id}@{reference.version}")
            references[key] = reference
        self.root = root.resolve()
        self.references = MappingProxyType(references)

    def resolve(self, resource_id: str, version: str, digest: str) -> ResourceReference:
        resource_id = _text(resource_id, "resource_id")
        version = _text(version, "version")
        digest = _text(digest, "digest")
        reference = self.references.get((resource_id, version))
        if reference is None:
            raise ResourceError(f"resource identity is unavailable: {resource_id}@{version}")
        if digest != reference.digest:
            raise ResourceError(f"resource digest conflicts with manifest: {resource_id}@{version}")
        return reference

    def read(self, resource_id: str, version: str, digest: str, *, max_bytes: int = 1_048_576) -> bytes:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ResourceError("resource read max_bytes must be a positive integer")
        reference = self.resolve(resource_id, version, digest)
        try:
            content = _read_regular_file(self.root, reference.path, max_bytes)
        except OSError as exc:
            raise ResourceError(f"resource read failed for {resource_id}@{version}: {exc}") from exc
        if "sha256:" + hashlib.sha256(content).hexdigest() != digest:
            raise ResourceError(f"resource bytes conflict with digest: {resource_id}@{version}")
        return content


def _read_regular_file(root: Path, relative: str, max_bytes: int) -> bytes:
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
        )
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ResourceError(f"resource must be a regular file: {relative}")
        if info.st_size > max_bytes:
            raise ResourceError(f"resource exceeds {max_bytes} byte read limit: {relative}")
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ResourceError(f"resource exceeds {max_bytes} byte read limit: {relative}")
    return content
