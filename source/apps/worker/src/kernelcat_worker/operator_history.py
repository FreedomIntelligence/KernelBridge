"""Finite administrative collect, reviewed admission and paged history reads."""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from kernelcat_executor.contracts.models import HarnessError, canonical
from kernelcat_ingestion.evidence import WikiEvidence
from kernelcat_ingestion.parsers.campaign import CampaignParser
from kernelcat_ingestion.pipeline import Pipeline, external_state
from kernelcat_ingestion.plans import Parsed
from kernelcat_ingestion.snapshots import DataError, install, sha256
from kernelcat_ingestion.sources.campaign import canonical_operator, read_manifest, source_plan
from kernelcat_knowledge.api.bootstrap import build_application
from kernelcat_knowledge.cards.history import (
    RULE_ID,
    RULE_VERSION,
    build_card,
    field_claims,
    rule_digest,
)
from kernelcat_knowledge.contracts.canonical import sha256_digest
from kernelcat_knowledge.corpus import CorpusError, initialize_corpus, open_evidence_store
from kernelcat_knowledge.generated.operator_history import HistoryDerivation

from kernelcat_worker.ingestion import IngestionEvidenceStore, JournalBinding
from kernelcat_worker.startup import PythonStartup, expected_input


def collect(startup: PythonStartup, manifest_path: Path, source_root: Path,
            state: Path, resources: Path, batch_time: str) -> dict[str, Any]:
    datetime.fromisoformat(batch_time.replace("Z", "+00:00"))
    manifest, raw = read_manifest(manifest_path)
    state = external_state(state)
    resources = external_state(resources)
    if state == resources or state.is_relative_to(resources) or resources.is_relative_to(state):
        raise DataError("history state and resource roots must be disjoint")
    if state.is_relative_to(source_root.resolve()) or resources.is_relative_to(source_root.resolve()):
        raise DataError("history outputs must not be inside source root")
    install(state / "batch.json", canonical({"time": batch_time, "rule": rule_digest()}).encode())
    install(state / "inputs/manifest.json", raw)
    initialize_corpus(resources)
    store = open_evidence_store(resources)
    plan = source_plan(manifest, raw, source_root, state / "inputs")
    pipeline = Pipeline(state / "acquisition", plan, effects=JournalBinding(state / "acquisition", startup),
                        evidence=WikiEvidence(resources, store=IngestionEvidenceStore(store)),
                        parser=CampaignParser(sha256(raw)))
    result = pipeline.collect()
    install(state / "collection.json", canonical(result).encode())
    if result["status"] != "completed":
        return {"status": "incomplete", "collection": result,
                "candidate_count": 0, "atlas": None}
    parsed = [Parsed.model_validate_json(canonical(pipeline.objects.get(row["parsed_ref"])))
              for row in result["inventory"]]
    cards, atlas = candidates(parsed, sha256(raw), batch_time)
    for operator, card in cards.items():
        install(state / "candidates" / (operator + ".json"), canonical(card).encode())
    install(state / "atlas.json", canonical(atlas).encode())
    return {"status": "completed", "collection": result, "candidate_count": len(cards),
            "atlas": str(state / "atlas.json"), "trial_rows": atlas["trial_rows"]}


def candidates(parsed: list[Parsed], manifest_digest: str,
               batch_time: str) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = parsed[0]
    if manifest.snapshot.source_key != "manifest" or len(manifest.elements) != 1:
        raise DataError("history manifest parse is unavailable")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parser_ids: dict[str, set[str]] = defaultdict(set)
    trial_rows = 0
    for item in parsed[1:]:
        origin = item.snapshot.name.split("/")[0]
        operator = canonical_operator(origin)
        elements = [element.model_dump(mode="json") for element in item.elements]
        groups[operator].extend(field_claims(operator, item.snapshot.name, elements))
        if len(groups[operator]) > 100000:
            raise DataError("operator history exceeds 100000 claims")
        parser_ids[operator].update(element.context["parser_identity"]["digest"]
                                    for element in item.elements)
        rows = len(elements) if item.snapshot.name.endswith(".jsonl") else 0
        trial_rows += rows
        sources[operator].append({"path": item.snapshot.name, "source_id": item.snapshot.source_id,
                                  "digest": item.snapshot.artifact_digest, "origin": origin,
                                  "trial_rows": rows})
    cards = {}
    entries = []
    for operator in sorted(groups):
        derivation: HistoryDerivation = {
            "kind": "derivation", "manifest_evidence_id": manifest.elements[0].evidence_id,
            "input_manifest_digest": manifest_digest,
            "parser_identity_digest": sha256_digest(sorted(parser_ids[operator])),
            "rule_id": RULE_ID, "rule_version": RULE_VERSION + ":" + rule_digest(),
        }
        card = build_card(operator, groups[operator], derivation, batch_time)
        if len(canonical(card).encode()) > 33554432:
            raise DataError("operator history card exceeds 32 MiB")
        cards[operator] = card
        entries.append({"operator": operator, "card_id": card["card_id"],
                        "revision_id": card["revision_id"], "sources": sources[operator]})
    atlas = {"version": 1, "manifest_digest": manifest_digest, "batch_time": batch_time,
             "aliases": {"conv2d": "conv_2d"}, "entries": entries, "trial_rows": trial_rows}
    return cards, atlas


def atlas_object(value: Any, fields: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DataError(location + ": expected an object")
    missing, extra = fields - value.keys(), value.keys() - fields
    if missing or extra:
        raise DataError(f"{location}: missing fields {sorted(missing)}; extra fields {sorted(extra)}")
    return value


def atlas_text(value: Any, pattern: str, location: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise DataError(location + ": invalid string identity")
    return value


def atlas_rows(value: Any, location: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise DataError(f"{location}: expected integer in 0..{maximum}")
    return value


def validate_atlas_source(value: Any, operator: str, location: str) -> dict[str, Any]:
    source = atlas_object(value, {"path", "source_id", "digest", "origin", "trial_rows"}, location)
    path = atlas_text(source["path"],
                      r"[a-z0-9_]+/(?:(?:card|strategies)\.json|trials_[a-z_]+\.jsonl)",
                      location + ".path")
    origin = atlas_text(source["origin"], r"[a-z0-9_]+", location + ".origin")
    if origin != path.split("/")[0] or canonical_operator(origin) != operator:
        raise DataError(location + ": source origin differs from operator/path")
    atlas_text(source["source_id"], r"src:data:[0-9a-f]{64}", location + ".source_id")
    atlas_text(source["digest"], r"sha256:[0-9a-f]{64}", location + ".digest")
    rows = atlas_rows(source["trial_rows"], location + ".trial_rows", 4096)
    if (path.endswith(".json") and rows != 0) or (path.endswith(".jsonl") and rows == 0):
        raise DataError(location + ": trial_rows differs from source format")
    return source


def validate_atlas_entry(value: Any, location: str) -> dict[str, Any]:
    entry = atlas_object(value, {"operator", "card_id", "revision_id", "sources"}, location)
    operator = atlas_text(entry["operator"], r"[a-z0-9_]+", location + ".operator")
    location += " (" + operator + ")"
    if canonical_operator(operator) != operator:
        raise DataError(location + ": operator must use its canonical identity")
    if entry["card_id"] != "kc:history:operator:" + operator + ":v1":
        raise DataError(location + ": card_id differs from operator")
    atlas_text(entry["revision_id"], r"rev:sha256:[0-9a-f]{64}", location + ".revision_id")
    sources = entry["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 175:
        raise DataError(location + ".sources: requires 1..175 sources")
    for index, source in enumerate(sources):
        validate_atlas_source(source, operator, f"{location}.sources[{index}]")
    return entry


def load_atlas(state: Path) -> dict[str, Any]:
    location = str(state / "atlas.json")
    atlas = atlas_object(bounded_json(state / "atlas.json"),
                         {"version", "manifest_digest", "batch_time", "aliases", "entries", "trial_rows"},
                         location)
    if type(atlas["version"]) is not int or atlas["version"] != 1:
        raise DataError(location + ": unsupported version")
    if atlas["aliases"] != {"conv2d": "conv_2d"}:
        raise DataError(location + ": unsupported aliases")
    atlas_text(atlas["manifest_digest"], r"sha256:[0-9a-f]{64}", location + ".manifest_digest")
    batch_time = atlas_text(atlas["batch_time"], r".+", location + ".batch_time")
    try:
        datetime.fromisoformat(batch_time.replace("Z", "+00:00"))
    except ValueError as error:
        raise DataError(location + ".batch_time: invalid timestamp") from error
    atlas_rows(atlas["trial_rows"], location + ".trial_rows", 175 * 4096)
    entries = atlas["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 53:
        raise DataError(location + ".entries: requires 1..53 operators")
    for index, entry in enumerate(entries):
        validate_atlas_entry(entry, f"{location}.entries[{index}]")
    validate_atlas_inventory(atlas, location)
    return atlas


def validate_atlas_inventory(atlas: dict[str, Any], location: str) -> None:
    # All shapes are validated before cross-entry comparisons or publication.
    identities = [entry["operator"] for entry in atlas["entries"]]
    if identities != sorted(set(identities)):
        raise DataError(location + ": operator identities must be sorted and unique")
    sources = [source for entry in atlas["entries"] for source in entry["sources"]]
    if len(sources) > 175:
        raise DataError(location + ": source inventory exceeds 175")
    for key in ("path", "source_id"):
        if len({source[key] for source in sources}) != len(sources):
            raise DataError(location + ": duplicate source " + key)
    if sum(source["trial_rows"] for source in sources) != atlas["trial_rows"]:
        raise DataError(location + ": trial_rows differs from source inventory")


def bounded_json(path: Path, cap: int = 33554432) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    if len(raw) > cap:
        raise DataError("history input exceeds byte bound: " + str(path))
    # Cards can be larger than an individual source; duplicate detection remains strict.
    import json

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(items)
        if len(result) != len(items):
            raise DataError("duplicate history input key: " + str(path))
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise DataError("invalid history JSON: " + str(path)) from error
    canonical(value)
    if not isinstance(value, dict):
        raise DataError("history input must be an object: " + str(path))
    return value


def admit(startup: PythonStartup, state: Path, resources: Path, *, reviewed: bool) -> dict[str, Any]:
    if not reviewed:
        raise DataError("operator history admission requires explicit --reviewed")
    state, resources = external_state(state), external_state(resources)
    atlas = load_atlas(state)
    app = build_application(resources, expected_modules=startup.knowledge_expected)
    results = []
    for entry in atlas["entries"]:
        path = state / "candidates" / (entry["operator"] + ".json")
        card = bounded_json(path)
        for identity in ("card_id", "revision_id"):
            if card.get(identity) != entry[identity]:
                raise DataError(f"operator history admission for {entry['operator']}: "
                                f"candidate {path} {identity} differs from atlas identity")
        result = app.card_publish(card, reviewed=True)
        if result["status"] not in {"published", "duplicate"}:
            raise DataError("history admission failed: " + canonical(result))
        results.append(result)
    return {"status": "admitted", "results": results}


def read(startup: PythonStartup, state: Path, resources: Path, operator: str,
         offset: int, limit: int) -> dict[str, Any]:
    state, resources = external_state(state), external_state(resources)
    atlas = load_atlas(state)
    operator = atlas["aliases"].get(operator, operator)
    entry = next((row for row in atlas["entries"] if row["operator"] == operator), None)
    if entry is None:
        raise DataError("operator absent from history atlas: " + operator)
    app = build_application(resources, expected_modules=startup.knowledge_expected)
    result = app.history_read(entry["card_id"], offset=offset, limit=limit)
    if result["revision_id"] != entry["revision_id"]:
        raise DataError("published history differs from atlas revision")
    return result


def arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kernelcat-worker operator-history", allow_abbrev=False)
    parser.add_argument("operation", choices=("collect", "admit", "read"))
    parser.add_argument("--expected-python-modules", type=Path)
    parser.add_argument("--startup-report", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--batch-time")
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--operator")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, expected_modules: object | None = None) -> int:
    args = arguments(argv)
    try:
        startup = PythonStartup.admit(expected_input(args.expected_python_modules, expected_modules))
        if not args.startup_report.is_absolute():
            raise DataError("startup report must be absolute")
        startup.export(args.startup_report)
        if args.operation == "collect":
            if args.manifest is None or args.source_root is None or args.batch_time is None:
                raise DataError("collect requires manifest, source-root and batch-time")
            result = collect(startup, args.manifest, args.source_root, args.state,
                             args.resources, args.batch_time)
        elif args.operation == "admit":
            result = admit(startup, args.state, args.resources, reviewed=args.reviewed)
        else:
            if args.operator is None:
                raise DataError("read requires operator")
            result = read(startup, args.state, args.resources, args.operator, args.offset, args.limit)
        print(canonical(result))
        return 0 if result["status"] != "incomplete" else 2
    except (OSError, ValueError, HarnessError, CorpusError) as error:
        print(canonical({"status": "error", "error": str(error)}), file=sys.stderr)
        return 2
