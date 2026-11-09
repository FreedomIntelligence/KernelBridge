"""Versioned advisory data. Profiles never grant tools or execute skill code."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from kernelcat_executor.contracts.base import Contract
from kernelcat_executor.contracts.models import canonical, digest


class LearnedSkill(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    description: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=8192)


class AgentProfile(Contract):
    schema_version: Literal["kernelagent.agent-profile/v1"] = "kernelagent.agent-profile/v1"
    guidance: str = Field(default="", max_length=8192)
    skills: tuple[LearnedSkill, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def bounded_unique_assets(self) -> AgentProfile:
        if len({s.name for s in self.skills}) != len(self.skills):
            raise ValueError("learned skill names must be unique")
        if len(canonical(self).encode("utf-8")) > 32768:
            raise ValueError("agent profile exceeds 32 KiB")
        return self


class ProfileRevision(Contract):
    scope: str = Field(min_length=1)
    generation: int = Field(ge=0)
    profile: AgentProfile
    parent_digest: str | None = None
    proposal_receipt: str | None = None
    validation_receipt: str | None = None

    def fingerprint(self) -> str:
        return digest(self)

    def projection(self) -> dict[str, object]:
        """Only skill metadata is automatic; full skill text is explicitly retrieved."""
        return {
            "scope": self.scope, "generation": self.generation,
            "revision_digest": self.fingerprint(), "guidance": self.profile.guidance,
            "skills": [{"name": s.name, "description": s.description,
                        "sha256": digest(s), "read_tool": "read_learned_skill"}
                       for s in self.profile.skills],
            "authority": "advisory_data_not_permissions",
        }
