"""Finite RPC for the TS Pi extension; no feature policy or next-turn decisions."""
from __future__ import annotations

from pathlib import Path

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.rsi_artifacts import replace_source, source_files
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.models import Action, Conflict, Json, Outcome, digest
from kernelcat_executor.contracts.rsi import validate_rsi
from kernelcat_executor.operations.gateway import Gateway

from kernelcat_worker.rsi_adapter import Adapter
from kernelcat_worker.rsi_lineage import Lineage
from kernelcat_worker.rsi_records import Records
from kernelcat_worker.rsi_rounds import RoundEffects


class RsiEffects:
    def __init__(self, gateway: Gateway, workspace: Workspace, artifacts: RuntimeArtifacts,
                 state: Path, task_id: str, config: Json) -> None:
        self.gateway, self.workspace, self.config = gateway, workspace, config
        self.records = Records(gateway.store, artifacts, task_id)
        self.rounds = RoundEffects(self.records)
        self.adapter = Adapter(config, workspace, artifacts, state)
        self.selected_step: int | None = None
        self.selected_context: list[Json] = []

    def status(self, step: int, stop: bool) -> Json:
        self.rounds.observe(step, stop)
        exposures, unknown = self._exposures()
        return validate_rsi({"task_id": self.records.task_id, "step": step, "stop": stop,
            "config": self.config, "files": source_files(self.workspace, self.config["source_paths"]),
            "record_sequence": len(self.records.entries()), "exposures": exposures,
            "exposure_unknown": unknown}, "RsiStatus")

    def _exposures(self) -> tuple[Json, Json]:
        observed: Json = {}
        unknown: Json = {}
        for item in self.gateway.store.snapshot()["actions"].values():
            action = item["action"]
            if action["task"] != self.records.task_id or action["tool"] != "__propose__":
                continue
            context = action["arguments"].get("rsi_context")
            if context is None:
                continue
            counts = observed if item.get("terminal") and item["outcome"]["status"] == "completed" else unknown
            for ku_id in set(context["ku_ids"]):
                counts[ku_id] = counts.get(ku_id, 0) + 1
        return observed, unknown

    def handle(self, method: str, request: Json, step: int) -> Json:
        if method == "rsi_bind_round":
            return self.rounds.bind(request)
        if method == "rsi_rounds":
            return self.rounds.page(request)
        if method == "rsi_list":
            return self.records.page(request)
        if method == "rsi_read":
            checked = validate_rsi(request, "RecordRead")
            return self.records.read(checked["record_id"])
        if method == "rsi_boundary":
            return Lineage(self.records).boundary(request)
        if method == "rsi_record":
            return self._record(request)
        if method == "rsi_effect":
            return self._effect(request)
        if method == "rsi_adapter":
            return self._adapter(request)
        if method == "rsi_apply":
            return self._apply(request, step)
        if method == "rsi_context":
            return self._context(request, step)
        raise Conflict("unknown finite RSI operation")

    def _record(self, request: Json) -> Json:
        action, record = self.records.prepare(request, "__rsi_record__")
        if record["kind"] in {"acceptance", "branch", "replay_request"}:
            raise Conflict("source acceptance and branch records require their original effects")
        if record["kind"] == "node":
            Lineage(self.records).check_node(record)
        if record["kind"] == "invalidation":
            self._check_invalidation(record)
        if record["kind"] == "round":
            self.rounds.check(record)
        if record["kind"] == "intervention":
            self.rounds.check_decision(record)
        result = self.gateway.internal(action, 0, lambda: self.records.outcome(record))
        return self.records.settled(result)

    def _check_invalidation(self, record: Json) -> None:
        request = validate_rsi(record["value"], "RsiInvalidation")
        identity = f"{self.records.task_id}/rsi/invalidation/{digest(request['measurement_id'])}"
        if record["record_id"] != identity:
            raise Conflict("RSI invalidation must identify its original measurement")
        for item in self.records.entries():
            original = self.records.decode(item)
            if (original["kind"] == "measurement"
                    and original["value"]["measurement"]["measurement_id"] == request["measurement_id"]):
                return
        raise Conflict("RSI invalidation has no original measurement in this task")

    def _effect(self, value: Json) -> Json:
        request = validate_rsi(value, "RsiEffectRequest")
        self._identity(request["identity"])
        action = Action(id=request["identity"], task=self.records.task_id,
                        tool=request["tool"], arguments=request["arguments"])
        return self.gateway.execute(action).model_dump(mode="json")

    def _adapter(self, value: Json) -> Json:
        request = validate_rsi(value, "AdapterCall")
        self._identity(request["identity"])
        if request["request"]["task_id"] != self.records.task_id:
            raise Conflict("RSI adapter request belongs to another task")
        if request["request"]["operation"] == "confirm":
            self._holdout_limit(request["identity"])
        return self.adapter.call(self.gateway, self.records.task_id,
                                 request["identity"], request["request"])

    def _holdout_limit(self, identity: str) -> None:
        actions = self.gateway.store.snapshot()["actions"]
        if identity in actions:
            return  # Gateway still verifies the exact original payload on replay.
        count = sum(item["action"]["task"] == self.records.task_id
                    and item["action"]["tool"] == "__rsi_adapter__"
                    and item["action"]["arguments"]["operation"] == "confirm"
                    for item in actions.values())
        # ExecutionSession owns the original task lock. A replacement Actor is
        # fenced again by RuntimeStore admission/start; UNKNOWN still counts.
        if count >= self.config["policy"]["holdout_limit"]:
            raise Conflict("RSI hold-out query limit exhausted")

    def _apply(self, value: Json, step: int) -> Json:
        request = validate_rsi(value, "SourceApply")
        original, record = self.records.prepare(request["record"], "__rsi_apply__")
        if record["kind"] != "acceptance" or record["value"].get("source") != request["replacement"]:
            raise Conflict("RSI applied bytes differ from the acceptance record")
        if (original.id not in self.gateway.store.snapshot()["actions"] and "turn_id" in record["value"]
                and record["value"]["turn_id"] != f"{self.records.task_id}/rsi/turn/{step}"):
            raise Conflict("RSI acceptance differs from the active turn")
        if {item["path"] for item in request["expected"]} != set(self.config["source_paths"]):
            raise Conflict("RSI source application differs from the configured path set")
        action = original.model_copy(update={"arguments": request})

        def effect() -> Outcome:
            replace_source(self.workspace, self.records.task_id, action.id,
                           request["expected"], request["replacement"])
            return self.records.outcome(record)

        return self.records.settled(self.gateway.internal(action, 0, effect))

    def _context(self, value: Json, step: int) -> Json:
        request = validate_rsi(value, "ContextSelection")
        expected = self.records.task_id + "/rsi/turn/" + str(step)
        if request["turn_id"] != expected:
            raise Conflict("RSI context selection refers to another turn")
        record = self.records.read(request["record_id"])
        if record["kind"] != "selection" or record["value"]["selection"]["turn_id"] != expected:
            raise Conflict("RSI context lacks its original selection record")
        selection = validate_rsi(record["value"]["selection"], "KnowledgeSelection")
        context = [validate_rsi(item, "KnowledgeContextItem") for item in record["value"]["context"]]
        if [item["ku_id"] for item in context] != [item["ku_id"] for item in selection["selected"]]:
            raise Conflict("RSI context differs from selected KU identities")
        self.selected_step, self.selected_context = step, context
        return {"selected": len(context)}

    def context(self, step: int) -> list[Json]:
        if self.selected_step != step:
            raise Conflict("native Pi RSI selection did not run for this model turn")
        return list(self.selected_context)

    def _identity(self, identity: str) -> None:
        if not identity.startswith(self.records.task_id + "/"):
            raise Conflict("RSI effect identity belongs to another task")

    def check_finished(self) -> None:
        records = [self.records.decode(item) for item in self.records.entries()]
        accepted = [item["value"] for item in records if item["kind"] == "acceptance"]
        settled = {item["experiment_id"] for item in accepted}
        if any(item["kind"] == "proposal" and item["value"]["experiment_id"] not in settled for item in records):
            raise Conflict("cannot finish with an unresolved RSI experiment")
        baseline = self.records.baseline()
        if digest(source_files(self.workspace, self.config["source_paths"])) != digest(baseline["value"]["source"]):
            raise Conflict("cannot finish with source that was not accepted by RSI")
