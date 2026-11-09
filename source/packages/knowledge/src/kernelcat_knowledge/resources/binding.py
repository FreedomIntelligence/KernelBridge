"""Immutable effective resources and the bounded retained configuration descriptor."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from kernelcat_knowledge.cards.authority import card_owner_roots
from kernelcat_knowledge.contracts import ContractError, canonical_json, sha256_digest
from kernelcat_knowledge.resources.configuration import (
    RepositoryPaths,
    load_repository_paths,
)

_LIMITS = frozenset({"raw_max_lines", "raw_max_bytes"})
_SELECTORS = frozenset(field.name for field in fields(RepositoryPaths)) - _LIMITS - {
    "repo_root",
    "config_path",
} | {"evidence_catalog", "sqlite_path"}


def validate_descriptor(value: Any) -> dict[str, Any]:
    """One internal format; no authority is inferred from a historical manifest."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "selectors",
        "limits",
    }:
        raise ContractError("invalid resource configuration descriptor")
    if value["schema_version"] != "kernelcat.resource-config/v1":
        raise ContractError("unsupported resource configuration version")
    selectors, limits = value["selectors"], value["limits"]
    if not isinstance(selectors, dict) or set(selectors) != _SELECTORS:
        raise ContractError("invalid resource configuration selectors")
    for key, path in selectors.items():
        if path is None and key in {"card_index_path", "rule_index_path"}:
            continue
        if not isinstance(path, str) or not path or "\\" in path:
            raise ContractError("invalid resource selector path")
        if path.startswith("file:"):
            parsed = urlsplit(path)
            if (
                parsed.netloc
                or parsed.query
                or parsed.fragment
                or not Path(unquote(parsed.path)).is_absolute()
                or Path(unquote(parsed.path)).as_uri() != path
            ):
                raise ContractError("resource selector URI must be canonical")
            continue
        if (
            Path(path).is_absolute()
            or ".." in Path(path).parts
            or Path(path).as_posix() != path
        ):
            raise ContractError("resource selector must be normalized")
    if not isinstance(limits, dict) or set(limits) != _LIMITS:
        raise ContractError("invalid resource configuration limits")
    if any(
        type(limits[key]) is not int or limits[key] < minimum
        for key, minimum in [("raw_max_lines", 1), ("raw_max_bytes", 1024)]
    ):
        raise ContractError("invalid resource configuration limit")
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > 32768:
        raise ContractError("resource configuration exceeds 32 KiB")
    return json.loads(encoded)


def normalized_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    return (
        resolved.relative_to(root).as_posix()
        if resolved.is_relative_to(root)
        else resolved.as_uri()
    )


@dataclass(frozen=True)
class ResourceBinding:
    paths: RepositoryPaths
    descriptor_json: str

    @classmethod
    def compose(cls, root: Path) -> ResourceBinding:
        paths = load_repository_paths(root)
        selectors = {}
        for name in _SELECTORS:
            path = (
                root / "data/evidence-catalog"
                if name == "evidence_catalog"
                else root / "artifacts/kcwiki/read-model.sqlite3"
                if name == "sqlite_path"
                else getattr(paths, name)
            )
            selectors[name] = (
                None if path is None else normalized_path(path, paths.repo_root)
            )
        descriptor = validate_descriptor(
            {
                "schema_version": "kernelcat.resource-config/v1",
                "selectors": selectors,
                "limits": {name: getattr(paths, name) for name in _LIMITS},
            }
        )
        return cls(paths, canonical_json(descriptor))

    @property
    def descriptor(self) -> dict[str, Any]:
        return json.loads(self.descriptor_json)

    @property
    def digest(self) -> str:
        return sha256_digest(self.descriptor)

    @property
    def card_roots(self) -> list[Path]:
        return [
            path
            for path in [
                self.paths.static_index_path,
                self.paths.dynamic_index_path,
                self.paths.card_index_path,
                self.paths.rule_index_path,
            ]
            if path is not None
        ]

    @property
    def writer_roots(self) -> list[Path]:
        cards = card_owner_roots(self.card_roots)
        return [
            path
            for path in [
                *cards,
                self.paths.source_records_dir,
                self.paths.repo_root / "data/evidence-catalog",
            ]
            if path is not None
        ]

    def require_sealable(self) -> None:
        cards = self.paths.card_index_path
        if cards is not None and cards.is_file():
            raise ContractError("configured card publication owner must be a directory")
        if any(
            value is not None and value.startswith("file:")
            for value in self.descriptor["selectors"].values()
        ):
            raise ContractError(
                "outside-root resources cannot be published or retained"
            )
