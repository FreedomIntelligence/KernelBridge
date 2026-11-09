"""Deterministic repository contract validation shared by CLI and tools."""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    validate_contract,
)
from kernelcat_knowledge.resources.configuration import load_repository_paths


def _objects(path: Path) -> Iterable[tuple[str, Any]]:
    if path.suffix == ".jsonl":
        for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if raw.strip():
                yield f"{path}:{line_no}", json.loads(raw)
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        for index, item in enumerate(payload, 1):
            yield f"{path}#{index}", item
    else:
        yield str(path), payload


def _default_contract_paths(repo_root: Path) -> list[Path]:
    """Discover authority objects from the repository's resolved configuration."""
    configured = load_repository_paths(repo_root)
    paths = [
        *sorted((repo_root / "wiki/cards").rglob("*.json")),
        *sorted(configured.source_records_dir.rglob("*.json")),
    ]
    bundle_root = configured.evidence_cas_dir / "contracts"
    if bundle_root.exists():
        paths.extend(sorted(path for path in bundle_root.rglob("*") if path.is_file()))
    return list(dict.fromkeys(paths))


def validate_repository(repo_root: Path, explicit: list[Path] | None = None) -> list[str]:
    errors: list[str] = []
    schema_dir = repo_root / "wiki/schemas"
    for path in sorted(schema_dir.glob("*.schema.json")):
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: invalid JSON Schema: {exc}")
            continue
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            errors.append(f"{path}: expected JSON Schema draft 2020-12")
        if not schema.get("$id") or not schema.get("title"):
            errors.append(f"{path}: $id and title are required")
        try:
            canonical_json(schema)
        except ValueError as exc:
            errors.append(f"{path}: non-canonical JSON value: {exc}")

    paths = explicit or _default_contract_paths(repo_root)
    for path in paths:
        if path.name.endswith(".schema.json"):
            continue
        try:
            objects = list(_objects(path))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: read/parse failed: {exc}")
            continue
        for locator, payload in objects:
            if not isinstance(payload, dict) or not str(payload.get("schema_version") or "").startswith("kernelcat."):
                errors.append(f"{locator}: expected a versioned KernelCat contract object")
                continue
            try:
                validate_contract(payload)
            except ContractError as exc:
                errors.append(f"{locator}: {exc}")
    return errors


def validation_report(repo_root: Path, explicit: list[Path] | None = None) -> dict[str, Any]:
    errors = validate_repository(repo_root, explicit)
    return {
        "schema_version": "kernelcat.validation-report/v1",
        "status": "ok" if not errors else "error",
        "errors": errors,
        "error_count": len(errors),
    }
