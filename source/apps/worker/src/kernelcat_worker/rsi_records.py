"""Bounded private record projection over the original effect Journal."""
from __future__ import annotations

import json

from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, Json, Outcome, canonical
from kernelcat_executor.contracts.ports import Store
from kernelcat_executor.contracts.rsi import validate_rsi

RECORD_TOOLS = frozenset({"__rsi_record__", "__rsi_apply__", "__rsi_branch__", "run_experiment", "replay"})


class Records:
    def __init__(self, store: Store, artifacts: RuntimeArtifacts, task_id: str) -> None:
        self.store, self.artifacts, self.task_id = store, artifacts, task_id
        self._page_entries: list[Json] | None = None
        self._next_cursor: int | None = None

    def entries(self) -> list[Json]:
        entries: list[Json] = []
        for item in self.store.snapshot()["actions"].values():
            if (item["action"]["task"] != self.task_id or item["action"]["tool"] not in RECORD_TOOLS
                    or not item.get("terminal") or item["outcome"]["status"] != "completed"):
                continue
            if item["action"]["tool"] == "replay" and "private_reference" not in item["outcome"]["data"]:
                continue
            if len(entries) >= 65536:
                raise Conflict("RSI record projection exceeds 65536 retained records")
            entries.append(item)
        return entries

    def decode(self, item: Json) -> Json:
        raw = self.artifacts.read(item["outcome"]["data"]["private_reference"])
        return validate_rsi(json.loads(raw), "PrivateRecord")

    def latest(self, kind: str) -> Json | None:
        for item in reversed(self.entries()):
            record = self.decode(item)
            if record["kind"] == kind:
                return record
        return None

    def baseline(self) -> Json:
        for item in reversed(self.entries()):
            record = self.decode(item)
            if record["kind"] in {"acceptance", "initial"}:
                return record
            if record["kind"] == "branch":
                baseline = self.read(record["value"]["baseline_record_id"])
                if baseline["kind"] not in {"acceptance", "initial"} or baseline["sequence"] >= record["sequence"]:
                    raise Conflict("RSI branch baseline must reference an earlier accepted source")
                return baseline
        raise Conflict("RSI baseline has not been initialized")

    def read(self, identity: str) -> Json:
        item = self.store.snapshot()["actions"].get(identity)
        if item is None or item["action"]["task"] != self.task_id or item["action"]["tool"] not in RECORD_TOOLS:
            raise Conflict("private RSI record not found for this task")
        if not item.get("terminal") or item["outcome"]["status"] == "unknown":
            raise InDoubt("private RSI record has an unresolved original operation")
        if item["outcome"]["status"] != "completed":
            raise Conflict("private RSI record did not complete")
        if "private_reference" not in item["outcome"]["data"]:
            raise Conflict("tool observation is not a private RSI record")
        return self.decode(item)

    def page(self, request: Json) -> Json:
        checked = validate_rsi(request, "RecordListRequest")
        cursor, limit = checked["cursor"], checked["limit"]
        # One immutable Journal snapshot per sequential page chain: O(E + R),
        # O(R) references. Writes and CAS still inspect the current owner state.
        if self._page_entries is None or cursor != self._next_cursor:
            self._page_entries = self.entries()
        entries = self._page_entries
        if cursor > len(entries):
            raise Conflict("RSI record cursor is beyond the original sequence")
        records: list[Json] = []
        byte_count = 0
        for item in entries[cursor:cursor + limit]:
            record = self.decode(item)
            size = len(canonical(record).encode())
            if size > 512 * 1024:
                raise Conflict("one RSI record exceeds the private page byte limit")
            if byte_count + size > 512 * 1024:
                break
            records.append(record)
            byte_count += size
        end = cursor + len(records)
        self._next_cursor = end if end < len(entries) else None
        if self._next_cursor is None:
            self._page_entries = None
        return validate_rsi({"records": records,
            "next_cursor": self._next_cursor, "sequence": len(entries)}, "RecordPage")

    def prepare(self, request: Json, tool: str) -> tuple[Action, Json]:
        checked = validate_rsi(request, "RecordWrite")
        identity = checked["record_id"]
        if not identity.startswith(self.task_id + "/"):
            raise Conflict("RSI record identity must be scoped to its original task")
        action = Action(id=identity, task=self.task_id, tool=tool, arguments=checked)
        previous = self.store.snapshot()["actions"].get(identity)
        if previous is None and checked["expected_sequence"] != len(self.entries()):
            raise Conflict("stale RSI record sequence")
        record = validate_rsi({"record_id": identity, "sequence": checked["expected_sequence"] + 1,
                              "kind": checked["kind"], "value": checked["value"]}, "PrivateRecord")
        if len(canonical(record).encode()) > 512 * 1024:
            raise Conflict("RSI record exceeds the private page byte limit")
        return action, record

    def outcome(self, record: Json) -> Outcome:
        raw = canonical(record).encode()
        if len(raw) > 512 * 1024:
            raise Conflict("RSI record exceeds the private page byte limit")
        reference = self.artifacts.put(raw, "rsi-private")
        return Outcome(data={"private_reference": reference})

    def settled(self, outcome: Outcome) -> Json:
        if outcome.status == "unknown":
            raise InDoubt("RSI original operation requires reconciliation")
        if outcome.status != "completed":
            raise Conflict("RSI original operation did not complete")
        return validate_rsi(json.loads(self.artifacts.read(outcome.data["private_reference"])), "PrivateRecord")
