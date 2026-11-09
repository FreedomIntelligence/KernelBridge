"""Fresh bounded reads of the actual card publication inputs, without loader caches."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    validate_knowledge_card,
)
from kernelcat_knowledge.storage.catalog_io import check_directory, read_bytes

MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_METADATA_ROWS = 16384
MAX_CARDS = 4096


def _files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    if root.is_file():
        yield root
        return
    check_directory(root)
    names: list[str] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name.endswith((".json", ".jsonl")):
                if len(names) >= MAX_METADATA_ROWS:
                    raise ContractError("card authority exceeds metadata file limit")
                names.append(entry.name)
    for name in sorted(names):
        yield root / name


def authority_rows(roots: list[Path]) -> Iterator[dict[str, Any]]:
    size = 0
    count = 0
    file_count = 0
    for root in roots:
        for path in _files(root):
            file_count += 1
            if file_count > MAX_METADATA_ROWS:
                raise ContractError("card authority exceeds metadata file limit")
            raw = read_bytes(path, MAX_RECORD_BYTES)
            size += len(raw)
            if size > MAX_TOTAL_BYTES:
                raise ContractError("card authority exceeds metadata byte limit")
            rows = _rows(path, raw)
            for row in rows:
                count += 1
                if count > MAX_METADATA_ROWS:
                    raise ContractError("card authority exceeds metadata row limit")
                if not isinstance(row, dict):
                    raise ContractError(f"{path}: card authority row must be an object")
                canonical_json(row).encode("utf-8")
                yield row


def _rows(path: Path, raw: bytes) -> Iterator[Any]:
    try:
        if path.suffix == ".jsonl":
            for line in raw.splitlines():
                if line.strip():
                    yield json.loads(line)
        else:
            payload = json.loads(raw)
            yield from payload if isinstance(payload, list) else [payload]
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError(f"{path}: invalid card authority JSON") from exc


def card_roots(repo_root: Path, card_root: Path) -> list[Path]:
    return [
        repo_root / "wiki/guide/static_index.jsonl",
        repo_root / "wiki/guide/dynamic_index.jsonl",
        card_root,
        repo_root / "wiki/rules/index.jsonl",
    ]


def card_owner_roots(roots: list[Path]) -> list[Path]:
    """Every directory whose publication can change the identity scan inputs."""
    return [
        path.parent
        if path.is_file() or (not path.exists() and path.suffix in {".json", ".jsonl"})
        else path
        for path in roots
    ]


def active_cards(roots: list[Path]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in authority_rows(roots):
        if row.get("status", "active") != "active":
            continue
        is_card = row.get("card_id") or row.get("type") == "operator_card"
        if row.get("schema_version") != "kernelcat.knowledge-card/v1":
            if is_card:
                raise ContractError(
                    "retained views do not support active legacy-only cards"
                )
            continue
        normalized = validate_knowledge_card(row)
        card_id = normalized["card_id"]
        if card_id in selected and canonical_json(selected[card_id]) != canonical_json(
            row
        ):
            raise ContractError(f"card {card_id}: conflicting active payloads")
        if card_id not in selected and len(selected) >= MAX_CARDS:
            raise ContractError("retained view exceeds 4096 active cards")
        selected[card_id] = row
    return [
        selected[key] for key in sorted(selected, key=lambda key: key.encode("utf-8"))
    ]


def existing_card(
    roots: list[Path], candidate: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Read accepted raw payloads; historical revisions also reserve their identities."""
    found = None
    for row in authority_rows(roots):
        if (row.get("card_id") or row.get("ID") or row.get("id")) != candidate[
            "card_id"
        ]:
            continue
        if row.get("revision_id") == candidate["revision_id"] and canonical_json(
            row
        ) != canonical_json(candidate):
            raise ContractError("immutable accepted card payload conflict")
        if row.get("status", "active") == "active":
            if found is not None and canonical_json(found) != canonical_json(row):
                raise ContractError("multiple active card payloads")
            found = row
    return found
