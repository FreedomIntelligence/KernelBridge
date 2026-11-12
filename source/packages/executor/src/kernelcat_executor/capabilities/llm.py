"""Direct strong-model API adapters. No vendor agent loop, shell, or hidden tools."""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from typing import Literal, cast

import httpx

from kernelcat_executor.capabilities.http import request_json
from kernelcat_executor.capabilities.messages import conversation, harness_conversation, parse_turn
from kernelcat_executor.capabilities.streaming import prepare_stream, request_stream
from kernelcat_executor.capabilities.task_instructions import TaskInstructions
from kernelcat_executor.contracts.context import ContextOverflow
from kernelcat_executor.contracts.messages import AssistantTurn
from kernelcat_executor.contracts.models import (
    Conflict,
    Json,
    Reply,
    ToolFailure,
    ToolSpec,
    canonical,
)
from kernelcat_executor.contracts.providers import DEEPSEEK_LEVELS, WireOptions, validate_endpoint
from kernelcat_executor.contracts.requests import ModelRequest
from kernelcat_executor.generated.harness import HarnessInput

SYSTEM = """You optimize kernels under a frozen task contract. Use the skill catalogue,
load relevant skills and references on demand, and make one tool call at a time.
You propose work; the harness alone authorizes execution. A tool failure is evidence,
not permission to bypass the harness. Never claim performance from static estimates.
Read existing source before writing; prefer edit_file for a small exact change.
Read/reference tools return next_offset; paginate instead of guessing omitted text.
Context is a lossy projection: read_receipt retrieves full evidence by action_id.
Never treat a JSON preview as a complete tool result. Keep unrelated changes frozen.
Finish with artifact paths, measured evidence, and limitations. No hidden shell exists.
"""


class APIProposer:
    """Supports OpenAI-compatible chat tools and Anthropic Messages tools.

    Inject an httpx.Client for tests. Automatic retries are intentionally disabled:
    every attempted provider call must get its own metered receipt.
    """
    def __init__(self, provider: str, base_url: str, model: str, api_key: str,
                 max_tokens: int = 8192, timeout: float = 120,
                 client: httpx.Client | None = None, *, wire_options: WireOptions | None = None,
                 allow_insecure_http: bool = False, max_response_bytes: int = 1048576,
                 task_instructions: TaskInstructions | None = None, max_retries: int = 0) -> None:
        if provider not in {"openai", "anthropic"}:
            raise ValueError("unsupported provider")
        if not model or not api_key:
            raise ValueError("an explicit model and API key are required")
        self.provider = cast(Literal["openai", "anthropic"], provider)
        self.model = model
        self.allow_insecure_http = allow_insecure_http
        self.wire_options = wire_options or WireOptions()
        if provider != "openai" and (self.wire_options.thinking is not None
                                     or self.wire_options.reasoning_effort is not None
                                     or self.wire_options.reasoning_levels):
            raise ValueError("explicit thinking control requires OpenAI-compatible transport")
        # Runtime selection (/model, /effort); None keeps the configured wire options.
        # The host records each change in the Journal and re-applies it on restart.
        self.reasoning: str | None = None
        # Transient failures the owner may repeat on this route (retry_policy).
        self.max_retries = max_retries
        self.max_response_bytes = max_response_bytes
        validate_endpoint(base_url, allow_insecure_http)
        if max_response_bytes <= 0:
            raise ValueError("provider response limit must be positive")
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.max_tokens, self.timeout = max_tokens, timeout
        self.client = client
        # Only trusted composition supplies this immutable input. Never promote
        # a field from model-provided context or delegate arguments into system.
        self._task_instruction_prompt = task_instructions.render() if task_instructions else ""

    def reasoning_levels(self) -> tuple[str, ...]:
        """Levels a user may select. Declared by the operator; DeepSeek's own endpoint
        has documented levels; any other endpoint offers none unless declared."""
        declared = self.wire_options.reasoning_levels
        if declared is not None or self.provider != "openai":
            return tuple(declared or ())
        return DEEPSEEK_LEVELS if urlsplit(self.base_url).hostname == "api.deepseek.com" else ()

    def configured_reasoning(self) -> str | None:
        """The level the configured wire options request; None is the provider default."""
        options = self.wire_options
        if options.thinking == "disabled":
            return "off"
        return options.reasoning_effort

    def effective_reasoning(self) -> str | None:
        return self.reasoning if self.reasoning is not None else self.configured_reasoning()

    def select_reasoning(self, level: str | None) -> None:
        """Select a declared level, or None to return to the configured options."""
        if level is not None and level not in self.reasoning_levels():
            raise ValueError(f"reasoning level {level!r} is not offered by this route")
        self.reasoning = level

    def _reasoning_messages(self, payload: Json) -> None:
        """Run on the final messages. With thinking off, no reasoning goes back. With
        thinking on (also DeepSeek's default when the field is absent), every assistant
        message of the current turn carries reasoning_content, empty if the model gave
        none: DeepSeek rejects a tool-call follow-up without the field (HTTP 400)."""
        if self.provider != "openai":
            return
        thinking = payload.get("thinking")
        messages = payload.get("messages", [])
        if thinking == {"type": "disabled"}:
            for message in messages:
                message.pop("reasoning_content", None)
            return
        if thinking != {"type": "enabled"} and "off" not in self.reasoning_levels():
            return
        last_user = max((index for index, item in enumerate(messages) if item.get("role") == "user"), default=-1)
        for message in messages[last_user + 1:]:
            if message.get("role") == "assistant":
                message.setdefault("reasoning_content", "")

    def _reasoning_fields(self, payload: Json) -> None:
        """thinking / reasoning_effort for the OpenAI-compatible request."""
        options = self.wire_options
        if self.reasoning is None:
            if options.thinking is not None:
                payload["thinking"] = {"type": options.thinking}
            if options.reasoning_effort is not None and options.thinking != "disabled":
                payload["reasoning_effort"] = options.reasoning_effort
            return
        # "off" exists only where thinking is a toggle; a level then switches it on.
        toggles = "off" in self.reasoning_levels()
        if self.reasoning == "off":
            payload["thinking"] = {"type": "disabled"}
            return
        if toggles:
            payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = self.reasoning

    def propose(self, context: Json, tools: tuple[ToolSpec, ...]) -> Reply | AssistantTurn:
        return self.invoke(self.prepare(context, tools))

    def prepare(self, context: Json, tools: tuple[ToolSpec, ...]) -> ModelRequest:
        """Pure assembly; the controller logs this before a transport can see it."""
        system = SYSTEM
        if self.wire_options.native_turns:
            system = system.replace("and make one tool call at a time.",
                                    "and return text or a bounded batch of tool calls.")
            system += "Use finish as the only call in its turn. Text completion never promotes a kernel.\n"
        if self._task_instruction_prompt:
            task_prompt = "\n" + self._task_instruction_prompt
            limit = context.get("policy", {}).get("context_bytes")
            if (type(limit) is int and len(canonical(context).encode("utf-8"))
                    + len(task_prompt.encode("utf-8")) > limit):
                raise ContextOverflow("child task instructions and context exceed context_bytes")
            system += task_prompt
        if self.provider == "openai":
            endpoint = "/chat/completions"
            headers = {"content-type": "application/json"}
            payload: Json = {
                "model": self.model, self.wire_options.token_parameter: self.max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": canonical(context)}],
                "tools": [{"type": "function", "function": {
                    "name": t.name, "description": t.description, "parameters": t.parameters,
                }} for t in tools],
                "tool_choice": self.wire_options.tool_choice,
            }
            if self.wire_options.include_parallel_tool_calls:
                payload["parallel_tool_calls"] = False
            self._reasoning_fields(payload)
        else:
            endpoint = "/messages"
            headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
            payload = {
                "model": self.model, "max_tokens": self.max_tokens, "system": system,
                "messages": [{"role": "user", "content": canonical(context)}],
                "tools": [{"name": t.name, "description": t.description,
                           "input_schema": t.parameters} for t in tools],
                "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
            }
        if self.wire_options.native_turns:
            messages = conversation(self.provider, context)
            if self.provider == "openai":
                payload["messages"] = [{"role": "system", "content": system}, *messages]
                if self.wire_options.include_parallel_tool_calls:
                    payload["parallel_tool_calls"] = True
            else:
                payload["system"], payload["messages"] = system, messages
                payload["tool_choice"] = {
                    "type": "any" if self.wire_options.tool_choice == "required" else "auto",
                    "disable_parallel_tool_use": False,
                }
            if not tools:
                for key in ("tools", "tool_choice", "parallel_tool_calls"):
                    payload.pop(key, None)
        self._reasoning_messages(payload)
        prepare_stream(payload, self.provider)
        return ModelRequest(provider=self.provider, endpoint=self.base_url + endpoint,
                            body_json=canonical(payload), public_headers_json=canonical(headers),
                            timeout=float(self.timeout), allow_insecure_http=self.allow_insecure_http)

    def invoke(self, request: ModelRequest) -> Reply | AssistantTurn:
        """Send exactly the admitted body; auth is added here and nowhere in the log."""
        return self._invoke(request, native_turns=self.wire_options.native_turns)

    def prepare_harness(self, harness: HarnessInput) -> ModelRequest:
        """Transport an already validated effective Pi projection, without a Host prompt."""
        if len(self.api_key) >= 12 and self.api_key in canonical(harness):
            raise ValueError("effective Pi input contains authentication material")
        messages = harness_conversation(self.provider, cast(list[Json], harness["messages"]))
        tools = harness["tools"]
        headers = {"content-type": "application/json"}
        if self.provider == "openai":
            endpoint = "/chat/completions"
            payload: Json = {
                "model": self.model, self.wire_options.token_parameter: self.max_tokens,
                "messages": [{"role": "system", "content": harness["system_prompt"]}, *messages],
                "tools": [{"type": "function", "function": tool} for tool in tools],
                "tool_choice": self.wire_options.tool_choice,
            }
            if self.wire_options.include_parallel_tool_calls:
                payload["parallel_tool_calls"] = True
            self._reasoning_fields(payload)
        else:
            endpoint = "/messages"
            headers["anthropic-version"] = "2023-06-01"
            payload = {"model": self.model, "max_tokens": self.max_tokens,
                       "system": harness["system_prompt"], "messages": messages,
                       "tools": [{"name": tool["name"], "description": tool["description"],
                                  "input_schema": tool["parameters"]} for tool in tools],
                       "tool_choice": {"type": "any" if self.wire_options.tool_choice == "required"
                                       else "auto", "disable_parallel_tool_use": False}}
        if not tools:
            for key in ("tools", "tool_choice", "parallel_tool_calls"):
                payload.pop(key, None)
        self._reasoning_messages(payload)
        prepare_stream(payload, self.provider)
        return ModelRequest(provider=self.provider, endpoint=self.base_url + endpoint,
                            body_json=canonical(payload), public_headers_json=canonical(headers),
                            timeout=float(self.timeout), allow_insecure_http=self.allow_insecure_http)

    def invoke_harness(self, request: ModelRequest) -> AssistantTurn:
        result = self._invoke(request, native_turns=True)
        assert isinstance(result, AssistantTurn)
        return result

    def _invoke(self, request: ModelRequest, *, native_turns: bool) -> Reply | AssistantTurn:
        request = ModelRequest.model_validate_json(canonical(request))
        endpoint = "/chat/completions" if self.provider == "openai" else "/messages"
        if (request.provider != self.provider or request.endpoint != self.base_url + endpoint
                or request.payload().get("model") != self.model
                or request.allow_insecure_http != self.allow_insecure_http):
            raise Conflict("prepared request does not match the configured model transport")
        headers = request.public_headers()
        headers.update({"authorization": f"Bearer {self.api_key}"} if self.provider == "openai"
                       else {"x-api-key": self.api_key})
        usage: Json = {}
        try:
            if request.payload().get("stream") is True:
                data = request_stream(request, headers, provider=self.provider,
                    max_bytes=self.max_response_bytes, client=self.client, secret=self.api_key)
            else:
                data = request_json("POST", request.endpoint, headers,
                    body=request.body_json.encode("utf-8"), timeout=request.timeout,
                    max_bytes=self.max_response_bytes, client=self.client)
            raw_usage = data.get("usage", {})
            if isinstance(raw_usage, dict):
                usage = {key: value for key, value in raw_usage.items()
                         if key in {"input_tokens", "output_tokens", "prompt_tokens",
                                    "completion_tokens", "total_tokens", "cache_read_input_tokens",
                                    "cache_creation_input_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                         and type(value) is int and value >= 0}
            if native_turns:
                turn = parse_turn(self.provider, data, usage)
                if len(self.api_key) >= 12 and self.api_key in canonical(turn):
                    raise ToolFailure("provider returned authentication material",
                                      details={"category": "credential_echo", "usage": usage})
                return turn
            if self.provider == "openai":
                choice = data["choices"][0]
                if choice.get("finish_reason") not in {None, "tool_calls", "stop"}:
                    raise ToolFailure("provider output was truncated or filtered",
                                      details={"usage": usage, "category": "incomplete_output"})
                calls = choice["message"].get("tool_calls", [])
                if len(calls) != 1:
                    raise ValueError("expected exactly one model tool call")
                function = calls[0]["function"]
                args = json.loads(function["arguments"])
                name = function["name"]
            else:
                if data.get("stop_reason") not in {None, "tool_use"}:
                    raise ToolFailure("provider did not finish a tool request",
                                      details={"usage": usage, "category": "incomplete_output"})
                calls = [b for b in data["content"] if b["type"] == "tool_use"]
                if len(calls) != 1:
                    raise ValueError("expected exactly one model tool call")
                name, args = calls[0]["name"], calls[0]["input"]
            if not isinstance(args, dict) or not isinstance(name, str) or not name:
                raise ValueError("invalid model tool request")
            encoded = canonical(args)
            if len(self.api_key) >= 12 and self.api_key in encoded:
                raise ToolFailure("provider returned authentication material",
                                  details={"category": "credential_echo", "usage": usage})
            return Reply(tool=name, arguments=args, usage=usage)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            raise ToolFailure("malformed provider response",
                              details={"usage": usage, "category": "protocol"}) from None

    def discover_models(self) -> Json:
        """One non-generating catalogue request; never select a billable model implicitly."""
        if self.provider != "openai":
            raise ValueError("model discovery currently supports OpenAI-compatible endpoints only")
        data = request_json("GET", self.base_url + "/models",
            {"authorization": f"Bearer {self.api_key}"}, timeout=self.timeout,
            max_bytes=self.max_response_bytes, client=self.client)
        entries = data.get("data")
        if not isinstance(entries, list) or any(not isinstance(e, dict)
                or not isinstance(e.get("id"), str) or not e["id"] for e in entries):
            raise ToolFailure("invalid model catalogue", details={"category": "protocol"})
        identifiers = sorted({e["id"] for e in entries})
        if len(self.api_key) >= 12 and any(self.api_key in name for name in identifiers):
            raise ToolFailure("provider returned authentication material",
                              details={"category": "credential_echo"})
        return {"model_ids": identifiers, "count": len(identifiers),
                "inference_called": False, "model_selected": None}
