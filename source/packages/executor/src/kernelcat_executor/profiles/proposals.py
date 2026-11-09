"""Proposal and independent validation contracts, separate from execution authority."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from kernelcat_executor.contracts.base import Contract
from kernelcat_executor.contracts.models import Json
from kernelcat_executor.contracts.profiles import AgentProfile, LearnedSkill


class Improvement(Contract):
    profile: AgentProfile | None = None
    skill: LearnedSkill | None = None
    rationale: str = Field(min_length=1, max_length=2048)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=16)


    @model_validator(mode="after")
    def one_target(self) -> Improvement:
        if (self.profile is None) == (self.skill is None):
            raise ValueError("supply exactly one of profile or skill")
        return self

    def resolve(self, baseline: AgentProfile) -> AgentProfile:
        """Upsert one skill without regenerating unrelated, already validated data."""
        if self.profile is not None:
            return self.profile
        skill = self.skill
        if skill is None:
            raise ValueError("missing skill")
        skills = [skill if old.name == skill.name else old for old in baseline.skills]
        if all(old.name != skill.name for old in baseline.skills):
            skills.append(skill)
        # Reconstruct instead of model_copy(update=...): count/byte limits still apply.
        return AgentProfile(guidance=baseline.guidance, skills=tuple(skills))


class ValidationVerdict(Contract):
    accepted: bool
    summary: str = Field(min_length=1, max_length=4096)
    metrics: Json = Field(default_factory=dict)
    # This label describes the trusted check, not a model's claim of a speedup.
    validation_kind: Literal["contract", "empirical"]
