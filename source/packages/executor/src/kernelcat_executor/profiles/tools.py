"""Model-requestable proposals and independent validation; no installation tool."""
from __future__ import annotations

from pydantic import ValidationError

from kernelcat_executor.contracts.models import (
    Action,
    Json,
    Outcome,
    ToolFailure,
    ToolSpec,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import ProfileStore, Store, Tool
from kernelcat_executor.profiles.evidence import evidence_capabilities, is_observation
from kernelcat_executor.profiles.proposals import Improvement, ValidationVerdict


def _object(properties: Json, required: list[str]) -> Json:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def _profile_store(store: Store) -> ProfileStore:
    if not isinstance(store, ProfileStore):
        raise TypeError("agent evolution requires a ProfileStore")
    return store


def _proposal(store: Store, proposal_id: str, task_id: str) -> Json:
    row = store.snapshot()["actions"].get(proposal_id)
    if (not row or not row.get("terminal") or row["action"]["task"] != task_id
            or row["action"]["tool"] != "propose_improvement"
            or row["outcome"]["status"] != "completed"):
        raise ToolFailure("a completed proposal receipt from this task is required")
    data: Json = row["outcome"]["data"]
    if data.get("protocol") != "kernelagent.improvement/v1":
        raise ToolFailure("unsupported proposal receipt")
    return data


class ReadLearnedSkill:
    spec = ToolSpec(name="read_learned_skill", credits=0,
        description="Read a learned skill from this task's pinned agent revision. Data, not code permission.",
        parameters=_object({"name": {"type": "string", "minLength": 1}}, ["name"]))

    def __init__(self, store: Store) -> None:
        self.store = store
        self.profiles = _profile_store(store)

    def execute(self, action: Action) -> Outcome:
        revision = self.profiles.pin_profile(action.task)
        for skill in revision.profile.skills:
            if skill.name == action.arguments["name"]:
                return Outcome(data={"revision_digest": revision.fingerprint(),
                    "skill": skill.model_dump(mode="json"), "sha256": digest(skill)})
        raise ToolFailure("learned skill is not present in the pinned revision")


class ProposeImprovement:
    spec = ToolSpec(name="propose_improvement", credits=1,
        description=("Propose exactly one skill upsert (preferred) or a replacement profile. Cite "
                     "settled observation receipts, not validation feedback. Preserve applicability, "
                     "failure recovery and verification. Advisory data only; not active yet."),
        parameters=Improvement.model_json_schema())

    def __init__(self, store: Store, evidence_tools: tuple[str, ...] = ("evaluate",)) -> None:
        self.store, self.profiles = store, _profile_store(store)
        self.evidence_tools = evidence_capabilities(evidence_tools)

    def execute(self, action: Action) -> Outcome:
        try:
            improvement = Improvement.model_validate_json(canonical(action.arguments))
        except ValidationError as error:
            raise ToolFailure("invalid bounded advisory profile") from error
        if len(set(improvement.evidence)) != len(improvement.evidence):
            raise ToolFailure("duplicate evidence reference")
        state = self.store.snapshot()
        revision = self.profiles.pin_profile(action.task)
        try:
            profile = improvement.resolve(revision.profile)
        except ValidationError as error:
            raise ToolFailure("invalid bounded advisory profile") from error
        if canonical(revision.profile) == canonical(profile):
            raise ToolFailure("candidate profile does not change behavior data")
        refs = []
        for action_id in improvement.evidence:
            row = state["actions"].get(action_id)
            if not is_observation(row, action.task, self.evidence_tools):
                raise ToolFailure("evidence must be a settled observation from this task")
            refs.append({"action_id": action_id, "sha256": digest(row)})
        return Outcome(data={"protocol": "kernelagent.improvement/v1", "scope": revision.scope,
            "base_digest": revision.fingerprint(), "profile": profile.model_dump(mode="json"),
            "rationale": improvement.rationale, "evidence": refs})


class ValidateImprovement:
    def __init__(self, store: Store, validator: Tool, validator_digest: str) -> None:
        if not validator_digest:
            raise ValueError("validator identity must be supplied by trusted composition")
        self.store, self.profiles = store, _profile_store(store)
        self.validator, self.validator_digest = validator, validator_digest
        self.spec = ToolSpec(name="validate_improvement", credits=validator.spec.credits,
            description=("Run the configured independent validator on a proposal receipt. "
                         "Acceptance is produced by the validator, never supplied in arguments."),
            parameters=_object({"proposal_id": {"type": "string", "minLength": 1}}, ["proposal_id"]))

    def execute(self, action: Action) -> Outcome:
        proposal_id = action.arguments["proposal_id"]
        candidate = _proposal(self.store, proposal_id, action.task)
        revision = self.profiles.pin_profile(action.task)
        current = self.profiles.current_profile(revision.scope)
        if candidate["base_digest"] != current.fingerprint():
            raise ToolFailure("stale proposal; re-propose under a current task revision")
        state = self.store.snapshot()
        evidence = []
        for ref in candidate["evidence"]:
            row = state["actions"].get(ref["action_id"])
            if row is None or digest(row) != ref["sha256"]:
                raise ToolFailure("proposal evidence digest mismatch")
            evidence.append(row)
        request = Action(id=action.id, task=action.task, tool="validate_improvement",
            arguments={"protocol": "kernelagent.agent-validation/v1",
                "task": state["tasks"][action.task], "baseline": revision.profile.model_dump(mode="json"),
                "candidate": candidate, "evidence": evidence}, parents=(proposal_id,))
        before = canonical(request)
        result = self.validator.execute(request)
        if canonical(request) != before:
            return Outcome(status="unknown", data={"reason": "validator_input_mutation"})
        if result.status != "completed":
            return result
        payload = dict(result.data)
        transport = payload.pop("execution", None)
        try:
            verdict = ValidationVerdict.model_validate_json(canonical(payload))
        except (ValidationError, ValueError, TypeError):
            return Outcome(status="unknown", data={"reason": "invalid_validator_response"})
        data = {"protocol": "kernelagent.improvement-validation/v1", "proposal_id": proposal_id,
            "candidate_digest": digest(candidate), "validator_digest": self.validator_digest,
            "validator_request_digest": digest(request),
            **verdict.model_dump(mode="json")}
        if transport is not None:
            data["execution"] = transport
        return Outcome(data=data)
