"""Read original settled turn identities; never run or restore an experiment."""
from __future__ import annotations

import json

from kernelcat_executor.contracts.harness import validate_replay
from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, canonical, digest
from kernelcat_executor.contracts.rsi import validate_rsi

from kernelcat_worker.rsi_records import Records


class Lineage:
    def __init__(self, records: Records) -> None:
        self.records = records

    def boundary(self, request: Json) -> Json:
        checked = validate_rsi(request, "RsiBoundaryRequest")
        acceptance = self.records.read(checked["acceptance_id"])
        if acceptance["kind"] != "acceptance":
            raise Conflict("RSI lineage requires an original acceptance")
        turn_id = acceptance["value"].get("turn_id")
        if turn_id is None:
            return {"boundary": None}  # Historical records did not retain a turn binding.
        prefix = self.records.task_id + "/rsi/turn/"
        if not isinstance(turn_id, str) or not turn_id.startswith(prefix):
            raise Conflict("RSI acceptance has an invalid turn binding")
        step = turn_id[len(prefix):]
        if not step.isascii() or not step.isdecimal() or str(int(step)) != step:
            raise Conflict("RSI acceptance has a noncanonical turn identity")
        actions = self.records.store.snapshot()["actions"]
        end = actions.get(f"{self.records.task_id}/step/{step}/end")
        if end is None:
            return {"boundary": None}
        self._completed(end, "__turn_end__")
        data = end["outcome"]["data"]
        if data["step"] != int(step) or "native_checkpoint" not in data:
            raise Conflict("RSI lineage requires the exact native turn checkpoint")
        model = actions.get(data["proposal_id"])
        if model is None:
            raise Conflict("RSI turn checkpoint has no original model request")
        self._completed(model, "__propose__")
        selection = self.records.read(turn_id + "/selection")
        return {"boundary": self._binding(acceptance, selection, model, data, turn_id)}

    def _completed(self, item: Json, tool: str) -> None:
        action = item["action"]
        if action["task"] != self.records.task_id or action["tool"] != tool:
            raise Conflict("RSI lineage references another task or effect kind")
        if not item.get("terminal") or item["outcome"]["status"] == "unknown":
            raise InDoubt("RSI lineage has an unresolved operation: " + action["id"])
        if item["outcome"]["status"] != "completed":
            raise Conflict("RSI lineage references a failed operation: " + action["id"])

    def _binding(self, acceptance: Json, selection: Json, model: Json,
                 end: Json, turn_id: str) -> Json:
        if selection["kind"] != "selection":
            raise Conflict("RSI lineage has no original knowledge selection")
        selected = validate_rsi(selection["value"]["selection"], "KnowledgeSelection")
        context = model["action"]["arguments"].get("rsi_context")
        identities = [item["ku_id"] for item in selected["selected"]]
        if (selected["turn_id"] != turn_id or context is None or context["ku_ids"] != identities
                or context["digest"] != digest(selection["value"]["context"])):
            raise Conflict("RSI lineage exposure differs from the original model input")
        checkpoint = validate_replay(end["native_checkpoint"], "NativeCheckpoint")
        return validate_rsi({"acceptance_id": acceptance["record_id"], "turn_id": turn_id,
            "proposal_id": model["action"]["id"],
            "model_input_digest": digest(model["action"]["arguments"]),
            "checkpoint_digest": checkpoint["digest"], "session_id": checkpoint["session_id"],
            "session_entry_id": checkpoint["leaf_id"], "selection": selected}, "RsiSettledBoundary")

    def check_node(self, record: Json) -> None:
        node = validate_rsi(record["value"], "RsiMeasuredNode")
        if record["record_id"] != node["node_id"] or node["node_id"] != node["acceptance_id"] + "/node":
            raise Conflict("RSI node must have its acceptance-derived immutable identity")
        boundary = self.boundary({"acceptance_id": node["acceptance_id"]})["boundary"]
        if boundary is None or canonical(boundary) != canonical(node["boundary"]):
            raise Conflict("RSI node does not match an acknowledged native boundary")
        original = self.records.read(node["acceptance_id"])
        self._check_measurement(node, original["value"])
        self._check_parent(node, original["sequence"])

    def _check_measurement(self, node: Json, acceptance: Json) -> None:
        measurement = validate_rsi(acceptance["confirmation"], "PairedMeasurement")
        accepted = acceptance["request"]["accept"]
        retained = digest(acceptance["source"])
        if (not measurement["independent_confirmation"] or not measurement["evidence_ids"]
                or accepted and measurement["remove_correct"] is not True):
            raise Conflict("RSI node requires an independently confirmed experiment")
        expected_retained = measurement["remove_digest"] if accepted else measurement["keep_digest"]
        if retained != expected_retained or node.get("retained_source_digest", retained) != retained:
            raise Conflict("RSI node retained source differs from its original decision")
        fields = {"task_id": self.records.task_id, "experiment_id": acceptance["experiment_id"],
                  "source_digest": measurement["remove_digest"], "baseline_digest": measurement["keep_digest"],
                  "accepted": acceptance["request"]["accept"], "measurement_id": measurement["measurement_id"],
                  "environment_id": measurement["environment_id"], "protocol_id": measurement["protocol_id"],
                  "correct": measurement["remove_correct"]}
        if any(node[key] != value for key, value in fields.items()):
            raise Conflict("RSI node differs from its original source or measurement")
        if (node["source_digest"] != measurement["remove_digest"]
                or node["score"]["samples"] != len(measurement["sample_ids"])):
            raise Conflict("RSI node does not identify the candidate measurement arm")
        self._check_source_commit(node, acceptance)

    def _check_source_commit(self, node: Json, acceptance: Json) -> None:
        identity = acceptance["experiment_id"] + "/adapter/confirm/" + digest("confirmation")
        original = self.records.store.snapshot()["actions"].get(identity)
        if original is None:
            if "source_commit" in node:
                raise Conflict("RSI node commit has no original adapter effect")
            return  # Historical nodes retain their weaker source-digest qualification.
        self._completed(original, "__rsi_adapter__")
        result = validate_rsi(json.loads(self.records.artifacts.read(
            original["outcome"]["data"]["private_reference"])), "AdapterResult")
        snapshot = result.get("source_snapshot")
        expected = snapshot["remove"] if snapshot else None
        if node.get("source_commit") != expected:
            raise Conflict("RSI node commit differs from its original evaluation checkout")

    def _check_parent(self, node: Json, acceptance_sequence: int) -> None:
        if node["parent_id"] is not None:
            parent = self.records.read(node["parent_id"])
            if (parent["kind"] != "node" or parent["value"]["task_id"] != node["task_id"]
                    or parent["value"]["accepted"] is not True
                    or parent["value"]["source_digest"] != node["baseline_digest"]):
                raise Conflict("RSI parent does not identify the measured baseline")
            parent_acceptance = self.records.read(parent["value"]["acceptance_id"])
            if parent_acceptance["sequence"] >= acceptance_sequence:
                raise Conflict("RSI parent must precede the accepted candidate")
        else:
            initial = self.records.read(self.records.task_id + "/rsi/initial")
            if initial["kind"] != "initial" or digest(initial["value"]["source"]) != node["baseline_digest"]:
                raise Conflict("RSI root node must extend the original initial baseline")
