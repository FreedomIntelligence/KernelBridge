"""Finite round provenance over admitted LoopX requests and original RSI records."""
from __future__ import annotations

from typing import cast

from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, digest
from kernelcat_executor.contracts.rsi import validate_rsi
from kernelcat_executor.operations.control import input_identity

from kernelcat_worker.rsi_records import Records


class RoundEffects:
    def __init__(self, records: Records) -> None:
        self.records = records
        self.request_id: str | None = None
        self.boundary: Json | None = None
        self._page: list[Json] | None = None
        self._cursor: int | None = None

    def bind(self, request: Json) -> Json:
        identity = validate_rsi(request, "RsiRoundSelect")["request_id"]
        self._request(identity, self.records.store.snapshot()["actions"])
        if self.request_id is not None and self.request_id != identity:
            raise Conflict("RSI process already has another LoopX round binding")
        self.request_id = identity
        return {"bound": True}

    def observe(self, step: int, stop: bool) -> None:
        if self.request_id is None or not stop:
            return
        actions = self.records.store.snapshot()["actions"]
        self.require_settled(actions)
        request = self._request(self.request_id, actions)
        self.boundary = self._boundary(self.request_id, request, step, actions)

    def _request(self, identity: str, actions: Json) -> Json:
        row = actions.get(identity)
        if (row is None or row["action"]["task"] != self.records.task_id
                or row["action"]["tool"] != "__loopx_turn_request__"):
            raise Conflict("RSI round has no original LoopX request")
        if not row.get("terminal") or row["outcome"]["status"] == "unknown":
            raise InDoubt("RSI round request requires reconciliation")
        if row["outcome"]["status"] != "completed":
            raise Conflict("RSI round request did not complete")
        return cast(Json, row["outcome"]["data"])

    def _boundary(self, identity: str, request: Json, end_step: int, actions: Json) -> Json:
        boundary = validate_rsi({"request_id": identity, "start_step": request["start_step"],
                                 "end_step": end_step}, "RsiRoundBoundary")
        if end_step < boundary["start_step"]:
            raise Conflict("RSI round end precedes its original start")
        if end_step > boundary["start_step"]:
            end = actions.get(f"{self.records.task_id}/step/{end_step - 1}/end")
            if (end is None or end["action"]["tool"] != "__turn_end__"
                    or not end.get("terminal") or end["outcome"]["status"] != "completed"):
                raise InDoubt("RSI round end has no acknowledged turn checkpoint")
            inputs = actions.get(f"{self.records.task_id}/step/{boundary['start_step']}/inputs")
            prompt = input_identity(self.records.task_id, identity.removesuffix("/request") + "/input")
            if (inputs is None or inputs["action"]["tool"] != "__input_batch__"
                    or not inputs.get("terminal") or inputs["outcome"]["status"] != "completed"
                    or prompt not in inputs["outcome"]["data"]["input_ids"]):
                raise Conflict("RSI round start did not consume its original LoopX instruction")
        return boundary

    def boundaries(self) -> list[Json]:
        actions = self.records.store.snapshot()["actions"]
        self.require_settled(actions)
        result: list[Json] = []
        # Original request order, not completion time or score. O(E) bounded scan.
        for identity, row in actions.items():
            if row["action"]["task"] != self.records.task_id or row["action"]["tool"] != "__loopx_turn_request__":
                continue
            request = self._request(identity, actions)
            candidate = actions.get(identity.removesuffix("/request") + "/result")
            if (candidate is not None and candidate["action"]["tool"] == "__loopx_turn_candidate__"
                    and candidate.get("terminal") and candidate["outcome"]["status"] == "completed"
                    and candidate["outcome"]["data"].get("round_mode") == "experiment"
                    and "end_step" in candidate["outcome"]["data"]):
                result.append(self._boundary(identity, request, candidate["outcome"]["data"]["end_step"], actions))
            elif self.boundary is not None and identity == self.request_id:
                result.append(self.boundary)
            if len(result) > 65536:
                raise Conflict("RSI round projection exceeds 65536 rounds")
        return result

    def require_settled(self, actions: Json) -> None:
        rows = [row for row in actions.values() if row["action"]["task"] == self.records.task_id]
        ended = {row["outcome"]["data"].get("proposal_id") for row in rows
                 if row["action"]["tool"] == "__turn_end__" and row.get("terminal")
                 and row["outcome"]["status"] == "completed"}
        for row in rows:
            if not row.get("terminal") or row["outcome"]["status"] == "unknown":
                raise InDoubt("RSI round has unresolved effects: " + row["action"]["id"])
            if (row["action"]["tool"] == "__propose__" and row["outcome"]["status"] == "completed"
                    and row["action"]["id"] not in ended):
                raise InDoubt("RSI round has an uncheckpointed model turn")
        private = [self.records.decode(row) for row in self.records.entries()]
        accepted = {row["value"]["experiment_id"] for row in private if row["kind"] == "acceptance"}
        if any(row["kind"] == "proposal" and row["value"]["experiment_id"] not in accepted for row in private):
            raise InDoubt("RSI round has an unsettled experiment proposal")

    def page(self, request: Json) -> Json:
        checked = validate_rsi(request, "RecordListRequest")
        cursor, limit = checked["cursor"], checked["limit"]
        if self._page is None or cursor != self._cursor:
            self._page = self.boundaries()
        if cursor > len(self._page):
            raise Conflict("RSI round cursor exceeds original round count")
        rounds = self._page[cursor:cursor + limit]
        end = cursor + len(rounds)
        self._cursor = end if end < len(self._page) else None
        if self._cursor is None:
            self._page = None
        return validate_rsi({"rounds": rounds, "next_cursor": self._cursor}, "RsiRoundPage")

    def check(self, record: Json) -> None:
        observed = validate_rsi(record["value"], "RsiRoundObservation")
        boundary = observed["boundary"]
        if record["record_id"] != boundary["request_id"] + "/observation" or boundary not in self.boundaries():
            raise Conflict("RSI round observation differs from its original boundary")
        start, end = boundary["start_step"], boundary["end_step"]
        prefix = self.records.task_id + "/rsi/turn/"
        nodes: list[str] = []
        acceptances: list[str] = []
        for item in self.records.entries():
            original = self.records.decode(item)
            kind = original["kind"]
            if kind not in {"node", "acceptance"}:
                continue
            turn = (original["value"]["boundary"]["turn_id"] if kind == "node"
                    else original["value"].get("turn_id", ""))
            if turn.startswith(prefix) and start <= int(turn[len(prefix):]) < end:
                (nodes if kind == "node" else acceptances).append(original["record_id"])
        if (observed["node_ids"] != nodes or observed["acceptance_ids"] != acceptances
                or observed["outcome"] != ("measured" if acceptances else "empty")):
            raise Conflict("RSI round observation omits or changes its measured attempts")

    def check_decision(self, record: Json) -> None:
        decision = validate_rsi(record["value"], "RsiRoundDecision")
        if not decision["round_ids"]:
            raise Conflict("RSI intervention requires original round observations")
        if record["record_id"] != decision["round_ids"][-1] + "/decision/" + digest(decision):
            raise Conflict("RSI intervention must retain its original decision identity")
        for identity in decision["round_ids"]:
            if self.records.read(identity)["kind"] != "round":
                raise Conflict("RSI intervention references a non-round record")
        for identity in decision["invalidation_ids"]:
            if self.records.read(identity)["kind"] != "invalidation":
                raise Conflict("RSI intervention references a non-invalidation record")
