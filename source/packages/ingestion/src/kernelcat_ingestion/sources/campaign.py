"""Pinned, portable campaign inventory admission; roots are explicit runtime inputs."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kernelcat_ingestion.parsers.native import strict_json
from kernelcat_ingestion.plans import Asset, SourcePlan, SourceSpec, relative_path
from kernelcat_ingestion.snapshots import DataError, sha256

ALIASES = {"conv2d": "conv_2d"}


def read_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    with path.open("rb") as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise DataError("history manifest exceeds 1 MiB")
    value = strict_json(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {"version", "audit_digest", "aliases", "files"}:
        raise DataError("invalid campaign manifest fields")
    if value["version"] != 1 or value["aliases"] != ALIASES:
        raise DataError("unsupported history manifest version or alias")
    rows = value["files"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 175:
        raise DataError("history manifest requires 1..175 source entries")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise DataError("invalid campaign source fields")
        path_value = relative_path(row["path"])
        if not re.fullmatch(r"[a-z0-9_]+/(?:card|strategies)\.json|[a-z0-9_]+/trials_[a-z_]+\.jsonl", path_value):
            raise DataError("unsupported campaign source path")
        if path_value in seen:
            raise DataError("duplicate campaign source path")
        seen.add(path_value)
        if type(row["bytes"]) is not int or not 0 < row["bytes"] <= 1048576:
            raise DataError("campaign source size outside 1..1 MiB")
        Asset(path=path_value, sha256=row["sha256"], role="document")
    return value, raw


def source_plan(manifest: dict[str, Any], raw: bytes, source_root: Path,
                manifest_root: Path) -> SourcePlan:
    pinned = sha256(raw)
    sources = (
        SourceSpec(key="manifest", kind="local", location=str(manifest_root.resolve()),
                   revision=pinned, source_class="historical_record", approval_ref=pinned,
                   assets=(Asset(path="manifest.json", sha256=pinned, role="document"),)),
        SourceSpec(key="campaign", kind="local", location=str(source_root.resolve()),
                   revision=pinned, source_class="historical_record", approval_ref=pinned,
                   assets=tuple(Asset(path=row["path"], sha256=row["sha256"], role="document")
                                for row in manifest["files"])),
    )
    count = len(manifest["files"]) + 1
    return SourcePlan(name="operator-history", sources=sources, requests=0,
                      max_file_bytes=1048576, bytes=count * 1048577, credits=4 * count)


def canonical_operator(origin: str) -> str:
    return ALIASES.get(origin, origin)
