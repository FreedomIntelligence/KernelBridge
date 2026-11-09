"""A bound view of the original owner; tool/model parameters never choose an Actor."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

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
from kernelcat_executor.contracts.profiles import AgentProfile, ProfileRevision
from kernelcat_executor.contracts.resources import resources
from kernelcat_executor.contracts.runtime import ContextAdmission, require
from kernelcat_executor.generated.context import ContextBudget
from kernelcat_executor.state.runtime import RuntimeJournal, atomic
from kernelcat_executor.state.runtime_context import context_event
from kernelcat_executor.state.runtime_state import (
    check_actor,
    check_expected,
    clone,
    pending,
    project,
    selected_record,
)


class RuntimeStore:
    concurrent_admission = True

    def __init__(self, owner: RuntimeJournal, actor: Json) -> None:
        self.owner, self.actor = owner, clone(actor)

    def runtime_state(self) -> Json:
        return self.owner.state(self.actor["run_id"], self.actor["trial_id"])

    def _check(self, state: Json, *, materialization: bool = False) -> Json:
        return check_actor(state, self.actor, self.owner.clock(), materialization=materialization)

    def _check_control(self, state: Json) -> None:
        self._check(state)

    def _scoped(self, state: Json) -> Json:
        record = selected_record(state, self.actor["run_id"], self.actor["trial_id"])
        state["policy"] = record["policy"]
        state["actions"] = {key: value for key, value in state["actions"].items()
            if value.get("binding", {}).get("run_id") == self.actor["run_id"] and
               value["binding"]["trial_id"] == self.actor["trial_id"]}
        state["requests"] = {key: value for key, value in state["requests"].items()
            if value.get("binding", {}).get("run_id") == self.actor["run_id"] and
               value["binding"]["trial_id"] == self.actor["trial_id"]}
        child_tasks = {action_id + "/leaf" for action_id, item in state["actions"].items()
                       if item["action"]["tool"] == "delegate"}
        state["tasks"] = {key: value for key, value in state["tasks"].items()
                          if key == record["task"]["id"] or key in child_tasks}
        knowledge = record["trial"]["external_snapshot"].get("knowledge")
        if knowledge is not None:
            state["runtime_knowledge"] = knowledge
        return state

    def snapshot(self) -> Json:
        return self._scoped(self.owner.snapshot())

    def events(self) -> list[Json]:
        state = self.snapshot()
        operation_ids = set(state["actions"]) | set(state["requests"])
        selected = []
        for row in self.owner._events(self.owner.events()):
            binding = row.get("binding", {})
            if (binding.get("run_id") == self.actor["run_id"]
                    and binding.get("trial_id") == self.actor["trial_id"]) or (
                    row.get("action_id") in operation_ids) or (
                    row.get("run_id") == self.actor["run_id"]):
                selected.append(row)
        return selected

    def policy(self) -> Policy:
        return Policy.model_validate_json(canonical(self.snapshot()["policy"]))

    @contextmanager
    def task_lock(self, task_id: str) -> Iterator[None]:
        # Concurrent opens of one assignment are excluded. A replacement has a new
        # key, while durable generation/epoch checks fence the old process.
        identity = self.actor["run_id"] + "/" + self.actor["session_id"] + "/" + str(self.actor["generation"])
        with self.owner.task_lock(identity):
            yield

    def register_task(self, task: Json) -> None:
        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            state = self.owner._state(rows)
            record = self._check(state)
            if task == record["task"]:
                return None, None
            parent_id = task["id"].removesuffix("/leaf")
            parent = state["actions"].get(parent_id)
            if (parent_id == task["id"] or parent is None or parent["action"]["tool"] != "delegate"
                    or not parent.get("started") or parent["binding"]["run_id"] != self.actor["run_id"]
                    or parent["binding"]["trial_id"] != self.actor["trial_id"]
                    or task["scope"] != record["task"]["scope"]
                    or task["contract"] != record["task"]["contract"] or task["seed_files"]):
                raise Conflict("only a bound admitted delegation may register a managed child task")
            old = state["tasks"].get(task["id"])
            if old is not None:
                require(old, task, "delegated Task")
                return None, None
            return {"kind": "task", "task": task, "binding": self.actor}, None
        self.owner._transaction(apply)

    def _check_action_scope(self, state: Json, action: Action) -> None:
        for records in (state["actions"], state["requests"]):
            previous = records.get(action.id)
            if previous is not None and any(previous.get("binding", {}).get(key) != self.actor[key]
                    for key in ("run_id", "trial_id")):
                raise Conflict("operation identity already belongs to another Run/Trial")

    def requested(self, action: Action) -> None:
        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            state = self.owner._state(rows)
            self._check(state)
            self._check_action_scope(state, action)
            state = self._scoped(state)
            self.owner._validate_request(state, action)
            if action.id in state["requests"]:
                return None, None
            return {"kind": "requested", "action": action.model_dump(mode="json"),
                    "action_digest": digest(action), "binding": self.actor}, None
        self.owner._transaction(apply)

    def admit(self, action: Action, credits: int, policy: Policy, *,
              expected_budget: ContextBudget | None = None,
              adoption: ContextAdmission | None = None) -> Json | None:
        return self.admit_resources(action, credits, policy, {}, expected_budget=expected_budget,
                                    adoption=adoption)

    def _check_input(self, state: Json, record: Json, action: Action) -> None:
        expected = action.arguments.get("expected")
        if expected is None:
            raise Conflict("managed input requires its expected ExecutionStamp")
        if (expected["run_id"] != self.actor["run_id"] or
                expected["trial_id"] != self.actor["trial_id"] or action.task != record["task"]["id"]):
            raise Conflict("managed input is outside the bound Run/Trial task")
        check_expected(state, expected)

    def admit_resources(self, action: Action, credits: int, policy: Policy,
                        quote: dict[str, int], *, expected_budget: ContextBudget | None = None,
                        adoption: ContextAdmission | None = None) -> Json | None:
        quote = resources(quote)
        if type(credits) is not int or credits < 0:
            raise ValueError("invalid credit charge")

        def apply(rows: list[Json]) -> tuple[Json | None, Json | None]:
            state = self.owner._state(rows)
            self._check_action_scope(state, action)
            previous = state["actions"].get(action.id)
            if action.tool == "__input__" and previous is not None:
                # Receipt lookup grants no new work, even after its stamp was
                # superseded. The original Actor/body and UNKNOWN guard remain.
                self._original(previous, action)
                return self.owner._admission(self._scoped(state), action, credits,
                                             policy, quote, expected_budget)
            record = self._check(state, materialization=action.tool == "__snapshot_program__")
            if action.tool == "__input__":
                self._check_input(state, record, action)
            # Admission reads this view. Serializing all per-Trial maps would
            # expand their shared retained graphs before every finite effect.
            scoped = self._scoped(dict(state))
            event, cached = self.owner._admission(scoped, action, credits, policy, quote, expected_budget)
            if event is None:
                return None, cached
            if action.tool == "__snapshot_program__":
                check_expected(state, action.arguments["expected"])
                if pending(state, self.actor["run_id"]):
                    raise InDoubt("workspace snapshot cannot overlap an unresolved effect")
            for domain in ("run_budget", "trial_budget"):
                limits = record[domain]
                if limits["spent"] + limits["reserved"] + credits > limits["limit"]:
                    raise BudgetExceeded(domain + " credit ceiling reached")
            changes: list[Json] = []
            if action.tool == "__propose__" and action.task == record["task"]["id"]:
                if adoption is None:
                    raise Conflict("managed parent model requires its atomic ContextCommit")
                changes.append(context_event(state, record, adoption))
                updated = {**record, **changes[-1]["changes"]}
                record = updated
            elif adoption is not None:
                raise Conflict("ContextCommit is only valid for its parent model operation")
            current = clone(cast(Json, project(state, self.actor["run_id"], self.actor["trial_id"])))["stamp"]
            binding = {**self.actor, **{key: current[key] for key in
                ("control_revision", "branch_revision", "context_revision", "trial_revision")}}
            if changes:
                binding.update(branch_revision=record["branch"]["revision"],
                    context_revision=record["branch"]["context_revision"],
                    trial_revision=record["trial"]["revision"])
            event["binding"] = binding
            return atomic([*changes, event]), None
        return self.owner._transaction(apply)

    def started(self, action: Action) -> None:
        def apply(rows: list[Json]) -> tuple[Json | None, Exception | None]:
            state = self.owner._state(rows)
            attempt = state["actions"].get(action.id)
            self._original(attempt, action)
            assert attempt is not None
            if attempt.get("terminal") or attempt.get("started") or attempt.get("outcome"):
                raise InDoubt("execution already started or settled; no second dispatch")
            try:
                self._check(state, materialization=action.tool == "__snapshot_program__")
                current = clone(cast(Json, project(state, self.actor["run_id"], self.actor["trial_id"])))["stamp"]
                for key in ("control_revision", "branch_revision", "context_revision", "trial_revision"):
                    if attempt["binding"][key] != current[key]:
                        raise Conflict("runtime domain revision changed before conditional start")
            except (Conflict, InDoubt) as error:
                return {"kind": "cancelled_unstarted", "action_id": action.id,
                    "outcome": {"status": "failed", "data": {"cancelled_unstarted": True,
                                "reason": str(error)}}}, error
            return {"kind": "started", "action_id": action.id, "binding": attempt["binding"]}, None
        error = self.owner._transaction(apply)
        if error is not None:
            raise error

    def _original(self, attempt: Json | None, action: Action) -> None:
        if attempt is None or attempt["action_digest"] != digest(action):
            raise Conflict("operation has no matching managed admission")
        if any(attempt["binding"][key] != value for key, value in self.actor.items()):
            raise Conflict("only the original bound Actor may settle/start this operation")

    def settle(self, action: Action, outcome: Outcome, elapsed_ms: float) -> None:
        outcome = Outcome.model_validate_json(canonical(outcome))

        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            state = self.owner._state(rows)
            attempt = state["actions"].get(action.id)
            self._original(attempt, action)
            assert attempt is not None
            if not attempt.get("started") and not attempt.get("terminal"):
                raise Conflict("unstarted managed operation cannot settle as performed")
            event = self.owner._settlement(state, action, outcome, elapsed_ms)
            return event, None
        self.owner._transaction(apply)

    def reject(self, action: Action, reason: str, policy: Policy) -> Outcome:
        def apply(rows: list[Json]) -> tuple[Json | None, Outcome]:
            state = self.owner._state(rows)
            self._check(state)
            self._check_action_scope(state, action)
            event, outcome = self.owner._rejection(self._scoped(state), action, reason, policy)
            if event is not None:
                event["binding"] = self.actor
            return event, outcome
        return self.owner._transaction(apply)

    def bind_profile(self, scope: str, profile: AgentProfile) -> None:
        if scope != self.snapshot()["tasks"][self._task_id()]["scope"]:
            raise Conflict("profile scope is outside the bound Run")
        self.owner.bind_profile(scope, profile, control_check=self._check_control)

    def _task_id(self) -> str:
        state = self.owner.snapshot()
        return str(selected_record(state, self.actor["run_id"], self.actor["trial_id"])["task"]["id"])

    def pin_profile(self, task_id: str) -> ProfileRevision:
        if task_id not in self.snapshot()["tasks"]:
            raise Conflict("profile Task is outside the bound Trial")
        return self.owner.pin_profile(task_id, control_check=self._check_control)

    def current_profile(self, scope: str) -> ProfileRevision:
        if scope != self.snapshot()["tasks"][self._task_id()]["scope"]:
            raise Conflict("profile scope is outside the bound Run")
        return self.owner.current_profile(scope)

    def activate_profile(self, proposal_id: str, validation_id: str, finish_id: str) -> ProfileRevision:
        return self.owner.activate_profile(proposal_id, validation_id, finish_id,
            control_check=self._check_control)

    def install(self, expected: str, policy: Policy, report: Json) -> None:
        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            self._check(self.owner._state(rows))
            raise Conflict("managed Run policy is frozen; create another explicit Run")
        self.owner._transaction(apply)
