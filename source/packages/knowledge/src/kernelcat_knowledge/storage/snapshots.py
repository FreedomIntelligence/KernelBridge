"""Explicit snapshot manifests referencing unchanged authority bytes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kernelcat_knowledge.contracts import canonical_json, sha256_digest
from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.ports import (
    EvidenceRepository,
    KnowledgeRepository,
    WikiSourceRepository,
)


def snapshot_create(
    output: Path,
    *,
    knowledge: KnowledgeRepository,
    sources: WikiSourceRepository,
    evidence: EvidenceRepository,
    card_root: Path,
    repo_root: Path,
) -> dict[str, Any]:
    knowledge.load_all()
    authority_roots = [
        card_root,
        getattr(sources, "root", repo_root / "data/source-catalog"),
        getattr(evidence, "root", repo_root / "data/evidence-catalog"),
    ]
    files: list[dict[str, Any]] = []
    for root in authority_roots:
        path = Path(root)
        if not path.is_dir():
            continue
        for file in sorted([*path.glob("*.json"), *path.glob("*.jsonl")]):
            files.append(
                {
                    "path": file.relative_to(repo_root).as_posix(),
                    "digest": "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest(),
                    "bytes": file.stat().st_size,
                }
            )
    manifest = {
        "schema_version": "kernelcat.snapshot/v1",
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "active_index_revision": knowledge.active_index_revision,
        "files": files,
    }
    manifest["digest"] = sha256_digest(manifest)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return response(
        "kernelcat.snapshot-operation/v1",
        "created",
        output=str(output),
        snapshot=manifest,
    )
