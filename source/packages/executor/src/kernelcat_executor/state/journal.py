"""JSONL authority: process-safe append transactions, fsync, hash chain.

Linux/macOS local filesystem only. No NFS claim. Each admission reads the ledger
under an OS lock, preventing concurrent overspend. Crashed reservations stay held.

Reads verify only what was appended since this process last verified the file.
The verified prefix (including this process's own appends) is anchored by file
identity, byte offset and the digest of its last record (re-hashed on every
transaction); it may only be extended. A shorter file, a changed anchor, or a
replacement that does not extend the verified history is an integrity failure.
The whole chain is re-verified on first use, on file replacement and every
FULL_VERIFY_SECONDS; until then an edit inside the prefix that keeps the file
size and the last record unchanged is not noticed. Callers receive fresh row
objects, so mutating them cannot alter the verified cache.

Every transaction takes LOCK_EX. flock gives a waiting writer no priority, so
shared readers polling status could hold admissions off for tens of seconds;
with the cache an exclusive read is short.
"""
from __future__ import annotations

import fcntl
import json
import marshal
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar

from kernelcat_executor.contracts.models import (
    Action,
    BudgetExceeded,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    Policy,
    canonical,
    digest,
)
from kernelcat_executor.contracts.python_module_observation import EVENT_KIND, observation_evidence
from kernelcat_executor.contracts.resources import resources
from kernelcat_executor.contracts.runtime import ContextAdmission
from kernelcat_executor.contracts.version_lock import validate_version_lock
from kernelcat_executor.generated.context import ContextBudget
from kernelcat_executor.generated.python_module_observation import PythonModuleObservationReceipt
from kernelcat_executor.state import python_module_observations

T = TypeVar("T")

# Upper bound on how long an edit inside the already-verified prefix that does
# not touch the last record can go unnoticed by a running process.
FULL_VERIFY_SECONDS = 300.0
GENESIS = "0" * 64
COMPACT_ROWS = 64
READ_CHUNK = 1 << 26  # macOS pread rejects reads of 2 GiB or more


@dataclass(frozen=True)
class _Verified:
    """A verified journal prefix: [0, offset) of the file with this identity."""
    identity: tuple[int, int]
    offset: int
    last_start: int
    last_digest: str
    count: int
    blob: bytes            # marshal of the first rows
    tail: tuple[bytes, ...]  # marshal of each later row, compacted into blob in batches
    verified_at: float

    def rows(self) -> list[Json]:
        rows: list[Json] = marshal.loads(self.blob)
        rows.extend(marshal.loads(item) for item in self.tail)
        return rows

    def extended(self, rows: list[Json], offset: int, last_start: int, last_digest: str) -> "_Verified":
        """This prefix plus rows (serialized now, before any caller can mutate them)."""
        tail = self.tail + tuple(marshal.dumps(row) for row in rows)
        blob = self.blob
        if len(tail) > COMPACT_ROWS:
            blob, tail = marshal.dumps(self.rows() + rows), ()
        return replace(self, offset=offset, last_start=last_start, last_digest=last_digest,
                       count=self.count + len(rows), blob=blob, tail=tail)


class Journal:
    # Each transaction opens and locks its own descriptor before reading state.
    concurrent_admission = True
    initial_kinds = frozenset({"initialized", "initialized_resources/v1"})

    def __init__(self, path: Path) -> None:
        self.path = path
        self._application_lease: tuple[str, str, int] | None = None
        # Admission provenance (call id, input, trial). The host executor binds
        # the shared value; helper threads it spawns inherit it. A concurrent
        # child-model thread binds a thread-local override so it never stamps
        # another request's call id into its admitted events.
        self._shared_context: Json = {}
        self._context_local = threading.local()
        self._verified: _Verified | None = None
        self._verified_lock = threading.Lock()

    @property
    def _application_context(self) -> Json:
        return getattr(self._context_local, "value", self._shared_context)

    def bind_application_context(self, context: Json, *, local: bool = False) -> None:
        value = json.loads(canonical(context))
        if local:
            self._context_local.value = value
        else:
            self._shared_context = value

    def application_state(self) -> Json | None:
        if self._application_lease is None:
            return None
        from kernelcat_executor.state.application import project
        identity = self._application_lease[0]
        return self._transaction(lambda rows: (None, project(rows, identity)), shared=True)

    def bind_application_lease(self, application_id: str, worker_id: str, epoch: int) -> None:
        """Bind by the trusted owner before exposing an execution RPC handle."""
        if not application_id or not worker_id or type(epoch) is not int or epoch < 1:
            raise ValueError("invalid application lease binding")
        self._application_lease = (application_id, worker_id, epoch)

    def _check_application_admission(self, rows: list[Json], event: Json) -> None:
        # All legacy/managed compound admissions pass this single commit point.
        effects = event["events"] if event["kind"] == "atomic" else [event]
        if not any(item["kind"] in {"admitted", "started"} for item in effects):
            return
        if self._application_lease is None:
            protected = [row["data"]["task"]["id"] for row in self._events(rows)
                         if row["kind"] == "application/created"]
            admitted = {row["action"]["id"]: row["action"] for row in self._events(rows) if row["kind"] == "admitted"}
            tasks = [item.get("action", admitted.get(item.get("action_id"), {})).get("task", "")
                     for item in effects if item["kind"] in {"admitted", "started"}]
            if any(value == task or value.startswith(task + "/") for value in tasks for task in protected):
                raise Conflict("application-owned task requires its host execution lease")
            return
        from kernelcat_executor.state.application import ModelAdmissionStopped, project
        identity, worker, epoch = self._application_lease
        state = project(rows, identity)
        lease = state["lease"]
        if not lease or lease["worker_id"] != worker or lease["lease_epoch"] != epoch:
            raise Conflict("execution lease expired before atomic admission")
        goal = state["goal"]
        model_admission = any(item["kind"] == "admitted" and item["action"]["tool"] == "__propose__" for item in effects)
        if model_admission and (state.get("takeover_requested") or state.get("pending_transition")):
            raise ModelAdmissionStopped("takeover" if state.get("takeover_requested") else "trial_transition")
        prior_admissions = {row["action"]["id"]: row for row in self._events(rows) if row["kind"] == "admitted"}
        for item in effects:
            if item["kind"] == "started":
                prior = prior_admissions.get(item.get("action_id"), {})
                if prior.get("application_session_id") == identity and prior.get("lease_epoch") != epoch:
                    raise Conflict("an unstarted action cannot cross execution leases")
        if goal and goal["status"] != "cleared" and any(item["kind"] == "admitted" and item["action"]["tool"] == "__propose__" for item in effects):
            if goal["status"] not in {"active", "cleared"} or state.get("takeover_requested") or state.get("pending_transition"):
                raise ModelAdmissionStopped(goal["status"])
            from kernelcat_executor.state.goal_progress import goal_usage
            usage = goal_usage(list(self._events(rows)), goal)
            consumed = usage["model_turns"]
            if goal.get("max_model_turns") is not None and consumed >= goal["max_model_turns"]:
                raise BudgetExceeded("Goal model-turn ceiling reached, including compaction")
            if goal.get("token_budget") is not None and usage["tokens"] >= goal["token_budget"]:
                raise BudgetExceeded("Goal token budget reached, including children and compaction")
        for item in effects:
            item.setdefault("application_session_id", identity)
            item.setdefault("lease_epoch", epoch)
            item.setdefault("application_context", self._application_context)

    @contextmanager
    def task_lock(self, task_id: str) -> Iterator[None]:
        """One cooperative controller per task; OS releases the lease on process death."""
        locks = self.path.parent / ".task-locks"
        locks.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(locks / digest(task_id), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict("task already has an active controller") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _validate_request(state: Json, action: Action) -> None:
        if action.task not in state["tasks"]:
            raise Conflict("action refers to an unregistered task")
        request = state["requests"].get(action.id)
        if request and request["action_digest"] != digest(action):
            raise Conflict("action differs from its durable request")
        for parent in action.parents:
            item = state["actions"].get(parent)
            if not item or not item.get("terminal"):
                raise Conflict("dependency is missing or unresolved")

    def _transaction(self, fn: Callable[[list[Json]], tuple[Json | None, T]], *, shared: bool = False) -> T:
        """Run fn on the verified rows under the file lock; append its event, if any.

        shared=True marks read-only work: the descriptor is read-only and an
        event from fn is refused. The lock is LOCK_EX either way (see module doc).
        """
        fd = os.open(self.path, os.O_RDONLY if shared else os.O_RDWR | os.O_APPEND)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            rows, previous, verified = self._verified_rows(fd)
            event, result = fn(rows)
            if event is not None:
                if shared:
                    raise RuntimeError("read-only journal transaction attempted to append")
                self._check_application_admission(rows, event)
                event = {**event, "seq": len(rows), "prev": previous}
                event["digest"] = digest(event)
                data = (canonical(event) + "\n").encode("utf-8")
                pending = memoryview(data)
                while pending:
                    pending = pending[os.write(fd, pending):]
                os.fsync(fd)
                # The durable append is part of what this process has verified, so a
                # later replacement that drops it is caught as not extending history.
                with self._verified_lock:
                    if self._verified is verified:
                        self._verified = verified.extended([event], verified.offset + len(data), verified.offset, event["digest"])
            return result
        finally:
            os.close(fd)

    @staticmethod
    def _parse_record(line: bytes, previous: str, seq: int) -> Json:
        if not line.endswith(b"\n"):
            raise Conflict("torn journal tail; offline repair required, refusing execution")
        try:
            row = json.loads(line)
            claimed = row.pop("digest")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Conflict("malformed journal record") from None
        try:
            valid = row.get("prev") == previous and digest(row) == claimed
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise Conflict("journal integrity failure")
        if row.get("seq") != seq:
            raise Conflict("non-contiguous event sequence")
        row["digest"] = claimed
        return row

    def _verified_rows(self, fd: int) -> tuple[list[Json], str, _Verified]:
        """Rows of the locked file, verifying only bytes past the cached verified prefix."""
        info = os.fstat(fd)
        identity, size, now = (info.st_dev, info.st_ino), info.st_size, time.monotonic()
        with self._verified_lock:
            cache = self._verified
        incremental = cache is not None and cache.identity == identity and now - cache.verified_at < FULL_VERIFY_SECONDS
        if incremental:
            assert cache is not None
            if size < cache.offset:
                raise Conflict("journal integrity failure: verified history was truncated")
            if cache.count:
                # Re-hash the last verified record: a rewrite that recomputed the chain changes it.
                anchor = os.pread(fd, cache.offset - cache.last_start, cache.last_start)
                try:
                    record = json.loads(anchor)
                    claimed = record.pop("digest")
                    unchanged = (anchor.endswith(b"\n") and claimed == cache.last_digest
                                 and record.get("seq") == cache.count - 1 and digest(record) == claimed)
                except (ValueError, KeyError, TypeError, AttributeError):
                    unchanged = False
                if not unchanged:
                    raise Conflict("journal integrity failure: verified history changed")
            base = cache
            rows: list[Json] = cache.rows()
        else:
            base = _Verified(identity, 0, 0, GENESIS, 0, marshal.dumps([]), (), now)
            rows = []
        offset, last_start, previous = base.offset, base.last_start, base.last_digest
        data = bytearray()
        while offset + len(data) < size:
            chunk = os.pread(fd, min(READ_CHUNK, size - offset - len(data)), offset + len(data))
            if not chunk:
                break
            data += chunk
        fresh: list[Json] = []
        start = 0
        while start < len(data):
            end = data.find(b"\n", start)
            line = bytes(data[start:] if end < 0 else data[start:end + 1])
            row = self._parse_record(line, previous, len(rows) + len(fresh))
            last_start, previous = offset + start, row["digest"]
            fresh.append(row)
            start += len(line)
        if not incremental and cache is not None and cache.count:
            # A re-read (interval or replaced file) must extend what this process already trusted.
            if len(fresh) < cache.count or fresh[cache.count - 1]["digest"] != cache.last_digest:
                raise Conflict("journal integrity failure: history does not extend the verified journal")
        verified = base if not fresh and incremental else base.extended(fresh, offset + len(data), last_start, previous)
        if verified is not cache:
            with self._verified_lock:
                current = self._verified
                if current is None or current.identity != identity or current.offset <= verified.offset:
                    self._verified = verified
        rows.extend(fresh)
        return rows, previous, verified

    @staticmethod
    def _events(rows: list[Json]) -> Iterator[Json]:
        """Compound domain changes share one durable hash-chain commit point."""
        for row in rows:
            if row["kind"] == "atomic":
                for event in row["events"]:
                    yield {**event, "seq": row["seq"]}
            else:
                yield row

    @staticmethod
    def _credit_event(budget: Json, row: Json, attempt: Json | None) -> None:
        """The same credit rules serve account, Run and Trial projections."""
        if row["kind"] == "admitted":
            budget["reserved"] += row["credits"]
        elif row["kind"] in {"settled", "cancelled_unstarted"}:
            assert attempt is not None
            budget["reserved"] -= attempt["credits"]
            if row["kind"] == "settled":
                budget["spent"] += attempt["credits"]

    @classmethod
    def _state(cls, rows: list[Json]) -> Json:
        if not rows or rows[0]["kind"] not in cls.initial_kinds:
            raise Conflict("campaign is not initialized for this ledger format")
        state: Json = {"limit": rows[0]["limit"], "spent": 0, "reserved": 0,
                       "policy": rows[0]["policy"], "actions": {}, "tasks": {}, "requests": {}}
        if rows[0]["kind"] == "initialized_resources/v1":
            limits = resources(rows[0]["resources"])
            state["resources"] = {"limits": limits, "spent": dict.fromkeys(limits, 0),
                                  "reserved": dict.fromkeys(limits, 0)}
        for row in cls._events(rows[1:]):
            kind = row["kind"]
            attempt = state["actions"].get(row.get("action_id"))
            cls._credit_event(state, row, attempt)
            if kind == "requested":
                state["requests"][row["action"]["id"]] = dict(row)
            elif kind == "started":
                state["actions"][row["action_id"]]["started"] = True
            elif kind == "admitted":
                for unit, amount in row.get("resources", {}).items():
                    state["resources"]["reserved"][unit] += amount
                state["actions"][row["action"]["id"]] = dict(row)
            elif kind in {"settled", "uncertain", "cancelled_unstarted"}:
                assert attempt is not None
                if kind in {"settled", "cancelled_unstarted"}:
                    for unit, amount in attempt.get("resources", {}).items():
                        state["resources"]["reserved"][unit] -= amount
                        if kind == "settled":
                            state["resources"]["spent"][unit] += row["resource_usage"][unit]
                attempt.update(outcome=row["outcome"], terminal=kind != "uncertain")
            elif kind == "rejected":
                state["actions"][row["action"]["id"]] = dict(row, terminal=True, credits=0)
            elif kind == "installed":
                state["policy"] = row["policy"]
            elif kind == "task":
                state["tasks"][row["task"]["id"]] = row["task"]
            elif kind == EVENT_KIND:
                python_module_observations.apply_observation(state, row)
        if rows[0]["kind"] in Journal.initial_kinds:
            python_module_observations.validate_owner_links(state, cls._events(rows), managed=False)
        return state

    def initialize(self, limit: int | None, policy: Policy, *,
                   resource_limits: dict[str, int] | None = None) -> None:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("credit limit must be a positive integer")

        limits = None if resource_limits is None else resources(resource_limits)
        initial = {"kind": "initialized", "limit": limit, "policy": policy.model_dump(mode="json")}
        if limits is not None:
            if not limits or not all(limits.values()):
                raise ValueError("resource limits must be nonempty and positive")
            initial.update(kind="initialized_resources/v1", resources=limits)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Only initialization may create state; status/replay are non-mutating reads.
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            if rows:
                if any(rows[0].get(key) != value for key, value in initial.items()):
                    raise Conflict("existing campaign has different initial settings")
                return None, None
            return initial, None
        self._transaction(apply)

    def register_task(self, task: Json) -> None:
        task = json.loads(canonical(task))

        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            tasks = self._state(rows)["tasks"]
            old = tasks.get(task["id"])
            for known in tasks.values():
                if known["scope"] == task["scope"] and known["contract"] != task["contract"]:
                    raise Conflict("one scope cannot denote different task contracts")
            if old is not None and old != task:
                raise Conflict("task ID reused with a different contract")
            if old is not None:
                return None, None
            event: Json = {"kind": "task", "task": task}
            runtime = next((row for row in rows if row["kind"] == "runtime"), None)
            if runtime is not None:
                event["runtime_fingerprint"] = runtime["fingerprint"]
                if "version_lock_digest" in runtime:
                    event["version_lock_digest"] = runtime["version_lock_digest"]
            return event, None
        self._transaction(apply)

    def bind_runtime(self, fingerprint: str | None = None, *, inputs: Json | None = None,
                     version_lock: object = None, probe_outputs: Json | None = None) -> str:
        """Bind one immutable runtime, retaining structured inputs for new campaigns."""
        if inputs is None and version_lock is None:
            if fingerprint is None or probe_outputs is not None:
                raise ValueError("legacy runtime binding requires only its fingerprint")
            binding: Json = {"kind": "runtime", "fingerprint": fingerprint}
        else:
            if inputs is None or version_lock is None or fingerprint is not None:
                raise ValueError("structured runtime binding requires inputs and VersionLock")
            frozen_inputs = json.loads(canonical(inputs))
            lock = validate_version_lock(version_lock)
            if lock["runtime_inputs_digest"] != "sha256:" + digest(frozen_inputs):
                raise ValueError("VersionLock does not identify the supplied runtime inputs")
            binding = {"kind": "runtime", "inputs": frozen_inputs, "version_lock": lock,
                       "version_lock_digest": "sha256:" + digest(lock),
                       "fingerprint": digest({"inputs": frozen_inputs, "version_lock": lock}),
                       "probe_outputs": json.loads(canonical(probe_outputs or {}))}

        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            self._state(rows)
            prior = next((r for r in rows if r["kind"] == "runtime"), None)
            if prior and "version_lock" in binding and "version_lock" not in prior:
                raise Conflict("legacy runtime has no structured VersionLock; start a new campaign")
            if prior and prior["fingerprint"] != binding["fingerprint"]:
                raise Conflict("runtime/config changed; start a new campaign")
            return (None if prior else binding), None
        self._transaction(apply)
        return str(binding["fingerprint"])

    def runtime_binding(self) -> Json | None:
        """Read the retained row; a legacy row explicitly lacks a VersionLock."""
        def read(rows: list[Json]) -> tuple[None, Json | None]:
            self._state(rows)
            return None, next((row for row in rows if row["kind"] == "runtime"), None)
        return self._transaction(read, shared=True)

    def record_python_modules(
        self, expected: object, report: object,
    ) -> PythonModuleObservationReceipt:
        """Retain trusted composition evidence after the existing campaign binding."""
        expected_value, report_value = observation_evidence(expected, report)

        def apply(rows: list[Json]) -> tuple[Json | None, PythonModuleObservationReceipt]:
            state = self._state(rows)
            if rows[0]["kind"] not in Journal.initial_kinds:
                raise Conflict("managed module observation requires an expected account revision")
            binding = python_module_observations.campaign_binding(rows)
            return python_module_observations.observation_event(
                state, len(rows), binding, expected_value, report_value,
                owner_events=self._events(rows))
        return self._transaction(apply)

    def python_module_observation(self, observation_id: str) -> PythonModuleObservationReceipt | None:
        """Read one exact observation; absence is unobserved, never inferred admission."""
        def read(rows: list[Json]) -> tuple[None, PythonModuleObservationReceipt | None]:
            state = self._state(rows)
            return None, state.get("python_module_observations", {}).get(observation_id)
        return self._transaction(read, shared=True)

    def requested(self, action: Action) -> None:
        action = Action.model_validate_json(canonical(action))

        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            state = self._state(rows)
            self._validate_request(state, action)
            if action.id in state["requests"]:
                return None, None
            return {"kind": "requested", "action": action.model_dump(mode="json"),
                    "action_digest": digest(action)}, None
        self._transaction(apply)

    def started(self, action: Action) -> None:
        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            item = self._state(rows)["actions"].get(action.id)
            if not item or item["action_digest"] != digest(action):
                raise Conflict("execution start has no matching admission")
            if item.get("terminal") or item.get("started") or item.get("outcome"):
                raise InDoubt("execution already started or settled; no second dispatch")
            return {"kind": "started", "action_id": action.id}, None
        self._transaction(apply)

    def events(self) -> list[Json]:
        return self._transaction(lambda rows: (None, rows), shared=True)

    def cancel_unstarted(self, action: Action) -> None:
        """Release only an original reservation proved never to have started."""
        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            prior = self._state(rows)["actions"].get(action.id)
            if not prior or prior["action_digest"] != digest(action):
                raise Conflict("cancellation does not match an original admission")
            if prior.get("terminal"):
                return None, None
            if prior.get("started") or prior.get("outcome"):
                raise InDoubt("started or uncertain effects require their original receipt")
            return {"kind": "cancelled_unstarted", "action_id": action.id,
                    "outcome": {"status": "failed", "data": {"cancelled_unstarted": True,
                        "recovered": True, "reason": "Original worker lost before execution start"}}}, None
        self._transaction(apply)

    def snapshot(self) -> Json:
        return self._transaction(lambda rows: (None, self._state(rows)), shared=True)

    def policy(self) -> Policy:
        return Policy.model_validate_json(canonical(self.snapshot()["policy"]))

    def admit(self, action: Action, credits: int, policy: Policy, *,
              expected_budget: ContextBudget | None = None,
              adoption: ContextAdmission | None = None) -> Json | None:
        return self.admit_resources(action, credits, policy, {}, expected_budget=expected_budget,
                                    adoption=adoption)

    def admit_resources(self, action: Action, credits: int, policy: Policy,
                        quote: dict[str, int], *,
                        expected_budget: ContextBudget | None = None,
                        adoption: ContextAdmission | None = None) -> Json | None:
        if adoption is not None:
            raise Conflict("legacy ledger cannot adopt managed ContextCommit")
        quote = resources(quote)
        expected_budget = expected_budget.copy() if expected_budget is not None else None
        if type(credits) is not int or credits < 0:
            raise ValueError("invalid credit charge")

        def apply(rows: list[Json]) -> tuple[Json | None, Json | None]:
            return self._admission(self._state(rows), action, credits, policy, quote, expected_budget)
        return self._transaction(apply)

    @staticmethod
    def _admission(state: Json, action: Action, credits: int, policy: Policy,
                   quote: dict[str, int], expected_budget: ContextBudget | None
                   ) -> tuple[Json | None, Json | None]:
        prior = state["actions"].get(action.id)
        if prior:
            if prior["action_digest"] != digest(action):
                raise Conflict("action ID reused with different inputs")
            if not prior.get("terminal"):
                raise InDoubt(f"{action.id}: reservation unresolved; do not retry")
            return None, prior["outcome"]
        if digest(state["policy"]) != digest(policy):
            raise Conflict("policy changed before admission")
        if expected_budget is not None and expected_budget != {
                key: state[key] for key in ("limit", "spent", "reserved")}:
            raise Conflict("budget changed before admission")
        Journal._validate_request(state, action)
        if state["limit"] is not None and state["spent"] + state["reserved"] + credits > state["limit"]:
            raise BudgetExceeded("campaign credit ceiling reached")
        if quote:
            account = state.get("resources")
            if account is None or any(k not in account["limits"] for k in quote):
                raise Conflict("resource limits are missing; start a resource campaign")
            if any(account["spent"][k] + account["reserved"][k] + n > account["limits"][k]
                   for k, n in quote.items()):
                raise BudgetExceeded("campaign resource ceiling reached")
        row = {"kind": "admitted", "action": action.model_dump(mode="json"),
               "action_digest": digest(action), "credits": credits,
               "policy_digest": digest(policy), "time_ns": time.time_ns()}
        if quote:
            row["resources"] = quote
        return row, None

    def settle(self, action: Action, outcome: Outcome, elapsed_ms: float) -> None:
        outcome = Outcome.model_validate_json(canonical(outcome))
        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            return self._settlement(self._state(rows), action, outcome, elapsed_ms), None
        self._transaction(apply)

    @staticmethod
    def _settlement(state: Json, action: Action, outcome: Outcome, elapsed_ms: float) -> Json | None:
        prior = state["actions"].get(action.id)
        if not prior or prior["action_digest"] != digest(action):
            raise Conflict("settlement does not match an admission")
        if prior.get("terminal"):
            if prior["outcome"] != outcome.model_dump(mode="json"):
                raise Conflict("conflicting terminal result")
            return None
        if prior.get("outcome") and outcome.status == "unknown":
            return None
        kind = "uncertain" if outcome.status == "unknown" else "settled"
        row = {"kind": kind, "action_id": action.id,
               "outcome": outcome.model_dump(mode="json"), "elapsed_ms": elapsed_ms, "time_ns": time.time_ns()}
        quote = prior.get("resources", {})
        if kind == "settled" and quote:
            usage = resources(outcome.data.get("resource_usage", quote))
            if set(usage) != set(quote) or any(usage[k] > quote[k] for k in quote):
                raise Conflict("invalid resource usage; reservation remains held")
            row.update(resource_usage=usage,
                       resource_basis="reported" if "resource_usage" in outcome.data else "bound")
        return row

    def reject(self, action: Action, reason: str, policy: Policy) -> Outcome:
        return self._transaction(lambda rows: self._rejection(self._state(rows), action, reason, policy))

    @staticmethod
    def _rejection(state: Json, action: Action, reason: str, policy: Policy
                   ) -> tuple[Json | None, Outcome]:
        outcome = Outcome(status="failed", data={"rejected": True, "reason": reason})
        prior = state["actions"].get(action.id)
        if prior:
            if prior["action_digest"] != digest(action):
                raise Conflict("action ID reused")
            if not prior.get("terminal"):
                raise InDoubt(action.id)
            return None, Outcome.model_validate_json(canonical(prior["outcome"]))
        Journal._validate_request(state, action)
        if digest(state["policy"]) != digest(policy):
            raise Conflict("policy changed before rejection")
        return {"kind": "rejected", "action": action.model_dump(mode="json"),
                "action_digest": digest(action), "policy_digest": digest(policy),
                "outcome": outcome.model_dump(mode="json")}, outcome

    def install(self, expected: str, policy: Policy, report: Json) -> None:
        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            old = self._state(rows)["policy"]
            if digest(old) != expected or policy.revision != old["revision"] + 1:
                raise Conflict("stale policy patch")
            return {"kind": "installed", "policy": policy.model_dump(mode="json"),
                    "base_digest": expected, "report": report}, None
        self._transaction(apply)
