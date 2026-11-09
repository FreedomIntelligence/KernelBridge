"""Finite source capture after original Gateway admission; TS decides its use."""
from __future__ import annotations

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.rsi_artifacts import source_files
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.models import Action, Json, Outcome, Policy, ToolSpec
from kernelcat_executor.contracts.ports import Store
from kernelcat_executor.contracts.rsi import validate_rsi
from kernelcat_executor.generated.rsi import SCHEMA
from kernelcat_executor.operations.registry import GateDecision

from kernelcat_worker.rsi_records import Records


class SourceGuard:
    name = "rsi-source-scope"

    def __init__(self, paths: list[str]) -> None:
        self.paths = frozenset(paths)

    def check(self, action: Action, scope: str, policy: Policy) -> GateDecision:
        if (action.tool in {"write_file", "edit_file"}
                and not action.id.startswith(action.task + "/seed/")
                and action.arguments.get("path") not in self.paths):
            return GateDecision(verdict="deny", reason="RSI writes require a configured source path")
        return GateDecision(verdict="abstain")


class ExperimentTool:
    spec = ToolSpec(name="run_experiment", credits=1,
        description=("Submit the current source for independent evaluation and simplification. "
                     "Provide a falsifiable hypothesis, expected improvement and registered KU IDs. "
                     "Use cites=[] when no registered knowledge was supplied; never invent an ID. "
                     "A submission is not correctness, timing or acceptance evidence."),
        parameters=SCHEMA["$defs"]["ExperimentProposal"])

    def __init__(self, store: Store, workspace: Workspace, artifacts: RuntimeArtifacts,
                 config: Json) -> None:
        self.store, self.workspace, self.artifacts, self.config = store, workspace, artifacts, config

    def execute(self, action: Action) -> Outcome:
        proposal = validate_rsi(action.arguments, "ExperimentProposal")
        records = Records(self.store, self.artifacts, action.task)
        known = set()
        for item in records.entries():
            retained = records.decode(item)
            if retained["kind"] == "candidates":
                candidates = validate_rsi(retained["value"]["candidates"], "RsiCandidates")
                known.update(candidate["ku_id"] for candidate in candidates["candidates"])
        unknown = sorted(set(proposal["cites"]) - known)
        if unknown:
            # Reject before writing a pending proposal. A model argument error
            # must not poison the protected settlement/checkpoint lifecycle.
            return Outcome(status="failed", data={"reason": "unknown_knowledge_identity",
                "unknown_cites": unknown, "submitted": False,
                "message": "Cite registered KU IDs only, or use cites=[] when none were supplied."})
        confirmations = sum(item["action"]["task"] == action.task
            and item["action"]["tool"] == "__rsi_adapter__"
            and item["action"]["arguments"].get("operation") == "confirm"
            for item in self.store.snapshot()["actions"].values())
        limit = self.config.get("policy", {}).get("holdout_limit")
        if limit is not None and confirmations >= limit:
            return Outcome(status="failed", data={"reason": "holdout_limit_exhausted",
                "submitted": False, "message": "The configured independent-confirmation budget is exhausted. Preserve the last accepted source and report this limit."})
        candidate = source_files(self.workspace, self.config["source_paths"])
        if candidate == records.baseline()["value"]["source"]:
            return Outcome(status="failed", data={"reason": "unchanged_source",
                "submitted": False,
                "message": "The current source equals the retained baseline. Use Pi edit/write to implement your hypothesis before submitting. After a rejected experiment, reread the restored source before editing."})
        record = validate_rsi({"record_id": action.id, "sequence": len(records.entries()) + 1,
            "kind": "proposal", "value": {"experiment_id": action.id, "proposal": proposal,
                "candidate": candidate,
                "integration": False}}, "PrivateRecord")
        outcome = records.outcome(record)
        return outcome.model_copy(update={"data": {**outcome.data,
            "experiment_id": action.id, "status": "submitted"}})
