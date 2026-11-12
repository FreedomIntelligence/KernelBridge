"""Operator-owned provider policy. Never infer capabilities by spending on retries."""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator

from kernelcat_executor.contracts.models import Contract


def validate_endpoint(value: str, allow_insecure_http: bool = False) -> str:
    """Validate an exact endpoint; HTTP authorization is explicit and local to it."""
    if not value or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("invalid provider URL")
    parsed = urlsplit(value)
    # Accessing port validates malformed/overflowing ports as well.
    _ = parsed.port
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("provider URL must be credential-free HTTP(S) without query or fragment")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not loopback and not allow_insecure_http:
        raise ValueError("remote HTTP requires this provider's explicit allow_insecure_http")
    return value.rstrip("/")


ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ReasoningLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
REASONING_ORDER = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# Documented thinking-mode effort levels of DeepSeek's own endpoint (V4: low, high, max).
DEEPSEEK_LEVELS = ("off", "low", "high", "max")


class WireOptions(Contract):
    """A small fixed compatibility vocabulary, not arbitrary request overrides."""

    token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    include_parallel_tool_calls: bool = True
    tool_choice: Literal["required", "auto"] = "required"
    # OpenAI-compatible thinking toggle ({"thinking": {"type": ...}}, DeepSeek) and the
    # top-level reasoning_effort. Reasoning text is display-only: it streams to the TUI
    # and is never part of the settled reply or of later requests.
    thinking: Literal["enabled", "disabled"] | None = None
    reasoning_effort: ReasoningEffort | None = None
    # Levels a user may select at runtime (/model, /effort). "off" disables thinking.
    # None: the documented levels of a known endpoint (DeepSeek), otherwise none.
    # An empty tuple turns runtime selection off for this route.
    reasoning_levels: tuple[ReasoningLevel, ...] | None = None

    # Opt-in until the rest of the interactive agent contract is implemented.
    native_turns: bool = False

    @field_validator("reasoning_levels")
    @classmethod
    def unique_levels(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("reasoning_levels must be unique")
        return value
