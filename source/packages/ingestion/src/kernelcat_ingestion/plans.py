"""Closed data contracts. Source/evidence IDs belong to Wiki, execution to Host."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from kernelcat_executor.contracts.base import Contract
from kernelcat_executor.contracts.models import Json
from pydantic import Field, model_validator

Key = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")]
Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Role = Literal["document", "implementation", "before", "after", "test", "config", "measurement", "log"]
Disposition = Literal["accepted", "partial", "needs_evidence", "parse_failed", "quarantined", "rejected"]


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value
            or path.as_posix() != value or any(ord(c) < 32 for c in value)):
        raise ValueError("path must be a normalized relative POSIX file path")
    return value


def public_url(value: str) -> None:
    p = urlsplit(value)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.query or p.fragment or p.port not in (None, 443)
            or re.search(r"%(?:2e|2f|5c|25)", p.path, re.I)
            or ".." in p.path.split("/") or any(ord(c) < 33 for c in value)):
        raise ValueError("expected credential-free HTTPS URL without query, fragment or traversal")


class Asset(Contract):
    path: str = Field(min_length=1)
    role: Role
    sha256: Sha256 | None = None

    @model_validator(mode="after")
    def safe_path(self) -> Asset:
        relative_path(self.path)
        return self


class SourceSpec(Contract):
    key: Key
    kind: Literal["local", "git", "https", "run"]
    location: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    source_class: str = Field(min_length=1)
    assets: tuple[Asset, ...] = Field(min_length=1, max_length=1000)
    approval_ref: str = Field(min_length=1)
    uses: tuple[Literal["archive", "train", "redistribute"], ...] = ("archive",)
    fixture: bool = False
    synthetic: bool = False

    @model_validator(mode="after")
    def pinned(self) -> SourceSpec:
        if len({a.path for a in self.assets}) != len(self.assets):
            raise ValueError("duplicate asset path")
        if "archive" not in self.uses or len(set(self.uses)) != len(self.uses):
            raise ValueError("an explicit archive grant and unique uses are required")
        if self.kind == "git":
            if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
                raise ValueError("Git revision must be a full commit SHA")
        elif any(a.sha256 is None for a in self.assets):
            raise ValueError("non-Git assets require reviewed content digests")
        if self.kind == "https":
            public_url(self.location)
            if not self.location.endswith("/"):
                raise ValueError("HTTPS source must end in / and defines the allowed prefix")
        elif not Path(self.location).is_absolute():
            raise ValueError("local source roots must be absolute")
        return self


class SourcePlan(Contract):
    """Pinned acquisition and parsing inputs, without dataset or learning actions."""

    schema_version: Literal["kernelcat.source-plan/v1"] = "kernelcat.source-plan/v1"
    name: Key
    sources: tuple[SourceSpec, ...] = Field(min_length=1, max_length=100)
    max_file_bytes: int = Field(default=1048576, gt=0, le=16777216)
    chunk_lines: int = Field(default=96, gt=0, le=1000)
    credits: int = Field(default=1000, gt=0)
    requests: int = Field(default=100, ge=0)
    bytes: int = Field(default=67108864, gt=0)

    @model_validator(mode="after")
    def unique_sources(self) -> SourcePlan:
        if len({source.key for source in self.sources}) != len(self.sources):
            raise ValueError("duplicate source key")
        return self


class Snapshot(Contract):
    source_id: str
    source_key: Key
    name: str
    revision: str
    artifact_ref: str
    artifact_digest: Sha256
    role: Role
    grant_digest: str
    size: int = Field(ge=0)


class Element(Contract):
    evidence_id: str
    source_id: str
    kind: Literal["code", "text", "table", "config", "log", "notebook_cell"]
    selector: Json
    text: str
    context: Json = Field(default_factory=dict)


class Parsed(Contract):
    schema_version: Literal["kernelcat.parsed/v1"] = "kernelcat.parsed/v1"
    parser: str = "native-lossless/v1"
    snapshot: Snapshot
    disposition: Disposition
    elements: tuple[Element, ...] = ()
    warnings: tuple[str, ...] = ()
