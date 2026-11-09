"""Finite source acquisition and parsing through the Host Gateway and Journal."""
from __future__ import annotations

import hashlib
import http.client
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kernelcat_executor.contracts.models import (
    Action,
    InDoubt,
    Json,
    Outcome,
    Task,
    ToolSpec,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import Tool

from kernelcat_ingestion.parsers.campaign import CampaignParser
from kernelcat_ingestion.parsers.native import NativeParser
from kernelcat_ingestion.plans import Asset, Parsed, Snapshot, SourcePlan, SourceSpec
from kernelcat_ingestion.ports import EffectBinding, EvidencePort
from kernelcat_ingestion.snapshots import DataError, Objects, install
from kernelcat_ingestion.sources.collect import acquire


@dataclass
class DataTool:
    spec: ToolSpec
    resources: dict[str, int]
    function: Callable[[Action], Outcome]

    def execute(self, action: Action) -> Outcome:
        try:
            return self.function(action)
        except (DataError, OSError, UnicodeError, http.client.HTTPException) as exc:
            # Without trustworthy actual use, Host charges the enforced quote.
            # Unexpected exceptions remain unknown and keep their reservation.
            return Outcome(status="failed", data={"reason": str(exc)})


def _spec(name: str, properties: Json) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Trusted source collection step",
        credits=1,
        parameters={
            "type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False,
        },
    )


def fingerprint() -> str:
    """Hash this parser/acquisition implementation; worker binds its own dependencies."""
    root = Path(__file__).parent
    files = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.rglob("*.py"))}
    return digest(files)


def external_state(path: Path) -> Path:
    root = path.resolve()
    if any((p / ".git").exists() or (p / "workspace.json").is_file() for p in (root, *root.parents)):
        raise DataError("data state must be outside the source checkout")
    return root


class Pipeline:
    def __init__(self, state: Path, plan: SourcePlan, *, effects: EffectBinding,
                 evidence: EvidencePort, authorize_network: bool = False,
                 parser: NativeParser | CampaignParser | None = None) -> None:
        self.root = external_state(state)
        self.plan = SourcePlan.model_validate_json(canonical(plan))
        if not self.plan.requests and any(source.kind == "https" for source in self.plan.sources):
            raise DataError("HTTPS collection requires a positive request quota")
        self.objects = Objects(self.root / "objects")
        self.evidence = evidence
        self.parser = parser if parser is not None else NativeParser()
        self.task = Task(
            id="data:" + digest(self.plan), goal="Collect and parse pinned source material",
            scope="data:" + self.plan.name, contract={"plan": digest(self.plan)},
        )
        tools: list[Tool] = [self._collector(source, authorize_network) for source in self.plan.sources]
        tools.append(DataTool(
            _spec("parse_source", {"snapshot_ref": {"type": "string"}}), {}, self._parse,
        ))
        limits = {"bytes": self.plan.bytes}
        if self.plan.requests:
            limits["requests"] = self.plan.requests
        self.effects = effects.bind(
            self.task, tools, credits=self.plan.credits, resource_limits=limits,
            runtime_digest=digest({"source": fingerprint(), "plan": digest(self.plan),
                                   "wiki_root": evidence.identity, "network": authorize_network,
                                   "parser_environment": self.parser.environment}),
        )
        install(self.root / "plan.json", canonical(self.plan).encode())

    def _collector(self, source: SourceSpec, network: bool) -> DataTool:
        assets = {asset.path: asset for asset in source.assets}
        quote = {"bytes": self.plan.max_file_bytes + 1}
        if self.plan.requests:
            quote["requests"] = 4 if source.kind == "https" else 0

        def collect(action: Action) -> Outcome:
            if source.kind == "https" and not network:
                raise DataError("network acquisition was not explicitly authorized")
            asset = assets[action.arguments["path"]]
            found = acquire(source, asset, self.plan.max_file_bytes)
            snapshot = self.evidence.capture(source, asset, found.content, found.origin)
            usage = {"bytes": len(found.content)}
            if self.plan.requests:
                usage["requests"] = found.requests
            return Outcome(data={
                "snapshot_ref": self.objects.put(snapshot),
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "resource_usage": usage,
            })

        return DataTool(_spec("collect_" + source.key, {"path": {"enum": list(assets)}}), quote, collect)

    def _parent_snapshots(self, action: Action) -> set[str]:
        rows = self.effects.snapshot()["actions"]
        found = set()
        for parent in action.parents:
            row = rows.get(parent)
            if (not row or not row.get("terminal") or row["action"]["task"] != self.task.id
                    or not row["action"]["tool"].startswith("collect_")
                    or row["outcome"]["status"] != "completed"):
                raise DataError("missing successful task-scoped producer receipt")
            found.add(row["outcome"]["data"]["snapshot_ref"])
        return found

    def _parse(self, action: Action) -> Outcome:
        if {action.arguments["snapshot_ref"]} != self._parent_snapshots(action):
            raise DataError("snapshot is not bound to its acquisition receipt")
        snapshot = Snapshot.model_validate_json(canonical(self.objects.get(action.arguments["snapshot_ref"])))
        raw = self.evidence.read(snapshot)
        parsed = self.parser.parse(snapshot, raw, self.plan.chunk_lines)
        for element in parsed.elements:
            self.evidence.register_element(snapshot, element)
        return Outcome(data={
            "parsed_ref": self.objects.put(parsed), "disposition": parsed.disposition,
            "elements": len(parsed.elements), "warnings": list(parsed.warnings),
        })

    def _call(self, step: str, tool: str, arguments: Json,
              parents: tuple[str, ...] = ()) -> tuple[str, Outcome]:
        action_id = self.task.id + "/" + step
        result = self.effects.execute(Action(
            id=action_id, task=self.task.id, tool=tool, arguments=arguments, parents=parents,
        ))
        if result.status == "unknown":
            raise InDoubt(action_id + ": reconcile before resuming")
        return action_id, result

    def _collect_asset(self, source: SourceSpec, asset: Asset) -> Json:
        key = source.key + "/" + asset.path
        receipt, acquired = self._call("collect/" + digest(key), "collect_" + source.key,
                                       {"path": asset.path})
        record: Json = {"asset": key, "receipt": receipt, "status": acquired.status}
        if acquired.status != "completed":
            return {**record, "disposition": "rejected", "reason": acquired.data}
        snapshot_ref = acquired.data["snapshot_ref"]
        snapshot = Snapshot.model_validate_json(canonical(self.objects.get(snapshot_ref)))
        parse_receipt, result = self._call("parse/" + digest(key), "parse_source",
                                           {"snapshot_ref": snapshot_ref}, (receipt,))
        record.update(snapshot_ref=snapshot_ref, parse_receipt=parse_receipt, parse_status=result.status)
        if result.status != "completed":
            self.evidence.read(snapshot)
            return {**record, "disposition": "parse_failed", "reason": result.data}
        parsed_ref = result.data["parsed_ref"]
        parsed = Parsed.model_validate_json(canonical(self.objects.get(parsed_ref)))
        if parsed.snapshot != snapshot:
            raise DataError("parsed object differs from its acquired snapshot")
        # Cached receipts reuse execution but never bypass live byte/evidence checks.
        self.evidence.check(snapshot, parsed.elements)
        record.update(parsed_ref=parsed_ref, disposition=parsed.disposition,
                      elements=len(parsed.elements), warnings=list(parsed.warnings))
        return record

    def collect(self) -> Json:
        with self.effects.task_lock(self.task.id):
            inventory = [self._collect_asset(source, asset)
                         for source in self.plan.sources for asset in source.assets]
            dispositions = {item["disposition"] for item in inventory}
            if dispositions & {"rejected", "parse_failed", "quarantined"}:
                status = "failed"
            elif dispositions == {"accepted"}:
                status = "completed"
            else:
                status = "partial"
            return {
                "status": status, "inventory": inventory,
                "coverage": "explicit_source_plan_inventory_only",
                "maturity": "source_derived", "target_validation": "not_performed",
                "budget": self.effects.snapshot()["resources"],
            }
