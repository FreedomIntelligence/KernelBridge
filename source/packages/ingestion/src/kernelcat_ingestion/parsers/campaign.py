"""Bounded campaign JSON/physical JSONL spans; historical records are never executed."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from kernelcat_executor.contracts.models import Json, canonical, digest

from kernelcat_ingestion.parsers.identity import observe_environment
from kernelcat_ingestion.parsers.native import strict_json
from kernelcat_ingestion.plans import Element, Parsed, Snapshot
from kernelcat_ingestion.snapshots import sha256

RECIPE = "campaign-history/v2"


@dataclass(frozen=True)
class CampaignParser:
    manifest_digest: str
    environment: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", canonical({
            "native": observe_environment(), "manifest": self.manifest_digest,
            "implementation": sha256(Path(__file__).read_bytes()), "recipe": RECIPE,
        }))

    def parse(self, snapshot: Snapshot, raw: bytes, chunk_lines: int = 96) -> Parsed:
        environment = json.loads(self.environment)
        if (environment["native"] != observe_environment()
                or environment["implementation"] != sha256(Path(__file__).read_bytes())):
            raise ValueError("campaign parser environment changed; create fresh state")
        identity = {"recipe": RECIPE, "environment": self.environment,
                    "artifact_digest": snapshot.artifact_digest, "name": snapshot.name}
        parser = RECIPE + ":" + canonical(identity)
        try:
            if len(raw) > 1048576:
                raise ValueError("campaign source exceeds 1 MiB")
            text = raw.decode("utf-8")
            is_lines = snapshot.name.endswith(".jsonl")
            lines = text.removesuffix("\n").split("\n") if text else []
            rows = list(enumerate(lines, 1)) if is_lines else [(0, text)]
            if not rows or len(rows) > 4096:
                raise ValueError("campaign source needs 1..4096 records")
            elements = []
            for line, content in rows:
                if is_lines:
                    content = content.removesuffix("\r")
                    if not content.strip(" \t\r"):
                        continue
                structure = strict_json(content)
                if not isinstance(structure, dict):
                    raise ValueError("campaign record must be an object")
                selector: Json = {"artifact_ref": snapshot.artifact_ref, "kind": "document"}
                if is_lines:
                    selector.update(kind="lines", start=line, end=line)
                eid = "ev:data:" + digest({"source": snapshot.source_id,
                                            "selector": selector, "parser": identity})
                elements.append(Element(
                    evidence_id=eid, source_id=snapshot.source_id, kind="config",
                    selector=selector, text=content, context={"structure": structure,
                    "parser_identity": {"recipe": RECIPE, "digest": digest(identity),
                                        "descriptor": identity}},
                ))
            if not elements:
                raise ValueError("campaign source contains no records")
            return Parsed(parser=parser, snapshot=snapshot, disposition="accepted",
                          elements=tuple(elements))
        except (UnicodeError, ValueError, RecursionError) as error:
            return Parsed(parser=parser, snapshot=snapshot, disposition="parse_failed",
                          warnings=(str(error),))
