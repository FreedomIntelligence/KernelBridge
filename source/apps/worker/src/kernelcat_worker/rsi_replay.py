"""Finite replay requests in the original Gateway; no session loop or native restore."""
from __future__ import annotations

from kernelcat_executor.contracts.models import Action, Conflict, Json, Outcome, ToolSpec
from kernelcat_executor.contracts.rsi import validate_rsi
from kernelcat_executor.generated.rsi import SCHEMA

from kernelcat_worker.rsi_records import Records


def replay_plugin(config: Json | None) -> Json | None:
    """Missing configuration is disabled; configuration was validated at its boundary."""
    if config is None or "replay_plugin" not in config:
        return None
    plugin: Json = config["replay_plugin"]
    return plugin if plugin["enabled"] else None


class ReplayTool:
    spec = ToolSpec(name="replay", credits=0,
        description=("Inspect recorded experiment nodes or request restoration of one adopted correct node. "
                     "Use list with a null target; replay requires an exact node ID and reason. "
                     "reset_context preserves code. Restoration happens after this settled session; "
                     "original effects and budget receipts are never repeated."),
        parameters=SCHEMA["$defs"]["ReplayToolArguments"])

    def __init__(self, records: Records) -> None:
        self.records = records

    def execute(self, action: Action) -> Outcome:
        request = validate_rsi(action.arguments, "ReplayToolArguments")
        if request["mode"] == "list":
            if request["target_node_id"] is not None:
                raise Conflict("replay list cannot select a target")
            return Outcome(data=self._nodes(request.get("cursor", 0)))
        if not request["reason"].strip():
            raise Conflict("replay restoration requires an explicit agent reason")
        if (request["mode"] == "replay") != (request["target_node_id"] is not None):
            raise Conflict("historical replay requires a node; context reset cannot select one")
        if request.get("cursor", 0) != 0:
            raise Conflict("replay restoration does not take a list cursor")
        value = validate_rsi({"action_id": action.id, "arguments": request}, "ReplayRequest")
        record = validate_rsi({"record_id": action.id, "sequence": len(self.records.entries()) + 1,
            "kind": "replay_request", "value": value}, "PrivateRecord")
        original = self.records.outcome(record)
        return original.model_copy(update={"data": {**original.data,
            "replay_request": value, "status": "requested", "restored": False}})

    def _nodes(self, cursor: int) -> Json:
        nodes = []
        for item in self.records.entries():
            record = self.records.decode(item)
            if record["kind"] == "node":
                value = validate_rsi(record["value"], "RsiMeasuredNode")
                nodes.append({key: value[key] for key in
                    ("node_id", "parent_id", "source_digest", "accepted", "correct", "measurement_id")})
        if cursor > len(nodes):
            raise Conflict("replay node cursor exceeds the original node count")
        end = min(cursor + 64, len(nodes))
        return {"nodes": nodes[cursor:end], "next_cursor": end if end < len(nodes) else None,
                "qualification": "Recorded observations; restoration rechecks original evidence and current authority"}


def check_request(records: Records, record: Json) -> Json:
    """Only an original admitted replay tool can produce a restoration request."""
    request = validate_rsi(record["value"], "ReplayRequest")
    original = records.store.snapshot()["actions"].get(request["action_id"])
    if (original is None or record["record_id"] != request["action_id"]
            or original["action"]["task"] != records.task_id
            or original["action"]["tool"] != "replay"
            or original["action"]["arguments"] != request["arguments"]
            or not original.get("terminal") or original["outcome"]["status"] != "completed"
            or original["outcome"]["data"].get("replay_request") != request
            or request["arguments"]["mode"] == "list"):
        raise Conflict("replay request lacks its original admitted Agent tool receipt")
    return request


def pending_request(records: Records) -> Json | None:
    requested = records.latest("replay_request")
    previous = records.latest("branch")
    if requested is None or previous is not None and previous["sequence"] >= requested["sequence"]:
        return None
    check_request(records, requested)
    return requested
