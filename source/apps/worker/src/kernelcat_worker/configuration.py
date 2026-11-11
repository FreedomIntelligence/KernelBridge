"""Closed operator configuration. No providers, process launches or state writes."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

import yaml
from kernelcat_executor.capabilities.pi import default_pi_root
from kernelcat_executor.capabilities.workspace_policy import validate_paths
from kernelcat_executor.contracts.agent import AgentOptions
from kernelcat_executor.contracts.base import Contract
from kernelcat_executor.contracts.models import Json, Policy, canonical
from kernelcat_executor.contracts.profiles import AgentProfile
from kernelcat_executor.contracts.providers import WireOptions
from kernelcat_executor.contracts.python_modules import validate_expected_python_modules
from kernelcat_executor.contracts.rsi import validate_policy, validate_rsi
from pydantic import Field, field_validator, model_validator


class ProviderConfig(Contract):
    provider: Literal["openai", "anthropic"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    allow_insecure_http: bool = False
    wire_options: WireOptions = Field(default_factory=WireOptions)
    max_response_bytes: int = Field(ge=4096, le=16777216, default=1048576)
    max_tokens: int = Field(gt=0, default=8192)
    # User-declared model context capacity, separate from execution budgets.
    context_window: int = Field(ge=4096, le=2097152, default=65536)
    timeout: float = Field(gt=0, default=120.0)


class EvaluatorConfig(Contract):
    argv: tuple[str, ...]
    parameters: Json
    credits: int = Field(gt=0, default=10)
    timeout: float = Field(gt=0, default=120.0)
    env_allowlist: tuple[str, ...] = ()


class ValidatorConfig(Contract):
    argv: tuple[str, ...] = Field(min_length=1)
    credits: int = Field(gt=0, default=10)
    timeout: float = Field(gt=0, default=120.0)
    env_allowlist: tuple[str, ...] = ()


class EvolutionConfig(Contract):
    # Missing validator deliberately means proposal-only mode, never auto-approval.
    validator: ValidatorConfig | None = None
    evidence_tools: tuple[str, ...] = Field(default=("evaluate",), min_length=1)


class PiWorkerConfig(Contract):
    """Required Pi SDK process configuration; no Python fallback."""
    node: str = Field(default_factory=lambda: os.environ.get("KERNELCAT_NODE") or shutil.which("node") or "")
    root: str = Field(default_factory=default_pi_root)
    role: Literal["coder", "scout"] = "coder"
    timeout: float = Field(gt=0, le=86400, default=300.0)
    max_frame_bytes: int = Field(ge=4096, le=16777216, default=1048576)
    codemode: bool = False


class NativeSessionSeed(Contract):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BusinessToolConfig(EvaluatorConfig):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=4096)
    files: tuple[NativeSessionSeed, ...] = Field(min_length=1, max_length=128)
    capture_paths: tuple[str, ...] = ()

    @field_validator("capture_paths")
    @classmethod
    def validate_capture_paths(cls, value):
        return validate_paths(value)


class CandidateRetention(Contract):
    tool: str
    source_hashes: dict[str, str] = Field(min_length=1)
    outcome_schema: Json


class RequiredCheck(Contract):
    tool: str
    before_tools: tuple[str, ...]
    outcome_schema: Json


class Config(Contract):
    python_modules: Json
    native_session_seed: NativeSessionSeed | None = None
    business_tools: tuple[BusinessToolConfig, ...] = Field(default=(), max_length=32)
    candidate_retention: CandidateRetention | None = None
    required_checks: tuple[RequiredCheck, ...] = ()
    state_dir: str = ".state"
    workspace: str = "workspace"
    workspace_mode: Literal["isolated", "project"] = "isolated"
    writable_paths: tuple[str, ...] | None = None
    readonly_paths: tuple[str, ...] = ("reference", "AGENTS.md", "AGENTS.task.md")
    knowledge_root: str | None = Field(default=None, min_length=1)
    skill_roots: tuple[str, ...] = ()
    credits: int | None = Field(gt=0, default=None)
    policy: Policy = Field(default_factory=Policy)
    agent_options: AgentOptions = Field(default_factory=AgentOptions)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    evaluator: EvaluatorConfig | None = None
    rsi: Json | None = None
    agent_profile: AgentProfile = Field(default_factory=AgentProfile)
    self_evolution: EvolutionConfig | None = None
    pi_worker: PiWorkerConfig = Field(default_factory=PiWorkerConfig)

    @model_validator(mode="after")
    def validate_evidence_configuration(self):
        from jsonschema import Draft202012Validator
        tools = {tool.name: tool for tool in self.business_tools}
        for check in self.required_checks:
            if check.tool not in tools:
                raise ValueError('required check must name a configured business tool')
            Draft202012Validator.check_schema(check.outcome_schema)
        if self.candidate_retention:
            retention = self.candidate_retention
            tool = tools.get(retention.tool)
            if self.rsi or tool is None or set(tool.capture_paths) != set(retention.source_hashes):
                raise ValueError('candidate retention requires one source-capturing tool and no RSI')
            if self.writable_paths is None or not set(retention.source_hashes) <= set(self.writable_paths):
                raise ValueError('retained sources must be explicitly writable task files')
            Draft202012Validator.check_schema(retention.outcome_schema)
        return self

    @field_validator("writable_paths", "readonly_paths")
    @classmethod
    def validate_workspace_paths(cls, value):
        return validate_paths(value)

    @field_validator("python_modules")
    @classmethod
    def validate_python_modules(cls, value: Json) -> Json:
        return dict(validate_expected_python_modules(value))

    @field_validator("rsi")
    @classmethod
    def validate_rsi_configuration(cls, value: Json | None) -> Json | None:
        if value is not None:
            value = validate_rsi(value, "RsiConfiguration")
            validate_policy(value["policy"])
        return value


def load_config(path: Path) -> Config:
    try:
        return Config.model_validate_json(canonical(yaml.safe_load(path.read_text())))
    except yaml.YAMLError:
        raise ValueError("invalid YAML configuration") from None
