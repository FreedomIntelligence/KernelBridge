"""Agent revisions in the existing hash-chained journal, not a second database."""
from __future__ import annotations

from collections.abc import Callable
from typing import cast

from kernelcat_executor.contracts.models import Conflict, Json, canonical, digest
from kernelcat_executor.contracts.profiles import AgentProfile, ProfileRevision
from kernelcat_executor.state.journal import Journal


def _completed(state: Json, action_id: str, tool: str) -> Json:
    row = state["actions"].get(action_id)
    if (not row or not row.get("terminal") or row["action"]["tool"] != tool
            or row["outcome"]["status"] != "completed"
            or row["outcome"]["data"].get("rejected")):
        raise Conflict(f"missing completed {tool} receipt")
    return cast(Json, row)


class AgentJournal(Journal):
    """An additive storage provider. Base ledger accounting and locking are unchanged."""

    @classmethod
    def _state(cls, rows: list[Json]) -> Json:
        state = super()._state(rows)
        state.update(agent_profiles={}, agent_initial_profiles={}, agent_pins={}, agent_activations={})
        for row in cls._events(rows):
            kind = row["kind"]
            if kind == "agent_profile_initialized":
                revision = row["revision"]
                state["agent_profiles"][revision["scope"]] = revision
                state["agent_initial_profiles"][revision["scope"]] = revision["profile"]
            elif kind == "agent_profile_pinned":
                state["agent_pins"][row["task_id"]] = row["revision"]
            elif kind == "agent_profile_activated":
                revision = row["revision"]
                state["agent_profiles"][revision["scope"]] = revision
                state["agent_activations"][revision["validation_receipt"]] = row
        return state

    def bind_profile(self, scope: str, profile: AgentProfile, *,
                     control_check: Callable[[Json], None] | None = None) -> None:
        initial = ProfileRevision(scope=scope, generation=0, profile=profile)

        def apply(rows: list[Json]) -> tuple[Json | None, None]:
            state = self._state(rows)
            if control_check is not None:
                control_check(state)
            old = state["agent_initial_profiles"].get(scope)
            if old is not None:
                if canonical(old) != canonical(profile):
                    raise Conflict("initial agent profile changed; start a new campaign")
                return None, None
            return {"kind": "agent_profile_initialized",
                    "revision": initial.model_dump(mode="json")}, None
        self._transaction(apply)

    def current_profile(self, scope: str) -> ProfileRevision:
        value = self.snapshot()["agent_profiles"].get(scope)
        if value is None:
            raise Conflict("agent profile scope is not initialized")
        return ProfileRevision.model_validate_json(canonical(value))

    def pin_profile(self, task_id: str, *,
                    control_check: Callable[[Json], None] | None = None) -> ProfileRevision:
        def apply(rows: list[Json]) -> tuple[Json | None, ProfileRevision]:
            state = self._state(rows)
            if control_check is not None:
                control_check(state)
            task = state["tasks"].get(task_id)
            if task is None:
                raise Conflict("cannot pin an unregistered task")
            old = state["agent_pins"].get(task_id)
            value = old if old is not None else state["agent_profiles"].get(task["scope"])
            if value is None:
                raise Conflict("agent profile scope is not initialized")
            revision = ProfileRevision.model_validate_json(canonical(value))
            event = None if old is not None else {
                "kind": "agent_profile_pinned", "task_id": task_id,
                "revision": revision.model_dump(mode="json"),
            }
            return event, revision
        return self._transaction(apply)

    def activate_profile(self, proposal_id: str, validation_id: str,
                         finish_id: str, *,
                         control_check: Callable[[Json], None] | None = None) -> ProfileRevision:
        """CAS activation rechecks authoritative receipts inside the journal lock."""
        def apply(rows: list[Json]) -> tuple[Json | None, ProfileRevision]:
            state = self._state(rows)
            if control_check is not None:
                control_check(state)
            proposal = _completed(state, proposal_id, "propose_improvement")
            validation = _completed(state, validation_id, "validate_improvement")
            finish = _completed(state, finish_id, "finish")
            task_id = proposal["action"]["task"]
            if any(r["action"]["task"] != task_id for r in (validation, finish)):
                raise Conflict("proposal, validation and finish must belong to one task")
            task = state["tasks"][task_id]
            candidate, verdict = proposal["outcome"]["data"], validation["outcome"]["data"]
            if (candidate.get("protocol") != "kernelagent.improvement/v1"
                    or candidate.get("scope") != task["scope"]
                    or verdict.get("protocol") != "kernelagent.improvement-validation/v1"
                    or verdict.get("accepted") is not True
                    or verdict.get("proposal_id") != proposal_id
                    or verdict.get("candidate_digest") != digest(candidate)
                    or not verdict.get("validator_digest")):
                raise Conflict("validation is not bound to this accepted candidate")
            old_activation = state["agent_activations"].get(validation_id)
            if old_activation is not None:
                if (old_activation["revision"]["proposal_receipt"] != proposal_id
                        or old_activation["finish_id"] != finish_id):
                    raise Conflict("activation identity reused")
                return None, ProfileRevision.model_validate_json(canonical(old_activation["revision"]))
            current = self.current_from_state(state, task["scope"])
            if candidate.get("base_digest") != current.fingerprint():
                raise Conflict("stale agent improvement; re-propose and re-validate")
            pinned = state["agent_pins"].get(task_id)
            if pinned is None or digest(pinned) != current.fingerprint():
                raise Conflict("task did not use the candidate's base revision")
            evidence = candidate.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise Conflict("candidate has no evidence")
            for ref in evidence:
                row = state["actions"].get(ref["action_id"])
                if (not row or not row.get("terminal")
                        or row["action"]["task"] != task_id
                        or digest(row) != ref["sha256"]
                        or row["outcome"]["data"].get("rejected")):
                    raise Conflict("candidate evidence changed or is not task-scoped")
            profile = AgentProfile.model_validate_json(canonical(candidate["profile"]))
            revision = ProfileRevision(scope=task["scope"], generation=current.generation + 1,
                profile=profile, parent_digest=current.fingerprint(),
                proposal_receipt=proposal_id, validation_receipt=validation_id)
            return {"kind": "agent_profile_activated", "finish_id": finish_id,
                    "revision": revision.model_dump(mode="json")}, revision
        return self._transaction(apply)

    @staticmethod
    def current_from_state(state: Json, scope: str) -> ProfileRevision:
        value = state["agent_profiles"].get(scope)
        if value is None:
            raise Conflict("agent profile scope is not initialized")
        return ProfileRevision.model_validate_json(canonical(value))
