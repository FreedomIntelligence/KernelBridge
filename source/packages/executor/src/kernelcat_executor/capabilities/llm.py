"""Direct strong-model API adapters. No vendor agent loop, shell, or hidden tools."""
from __future__ import annotations

import json
from typing import Literal, cast

import httpx

from kernelcat_executor.capabilities.http import request_json
from kernelcat_executor.capabilities.messages import conversation, harness_conversation, parse_turn
from kernelcat_executor.contracts.messages import AssistantTurn
from kernelcat_executor.contracts.models import (
    Conflict,
    Json,
    Reply,
    ToolFailure,
    ToolSpec,
    canonical,
)
from kernelcat_executor.contracts.providers import WireOptions, validate_endpoint
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
                 allow_insecure_http: bool = False, max_response_bytes: int = 1048576) -> None:
        if provider not in {"openai", "anthropic"}:
            raise ValueError("unsupported provider")
        if not model or not api_key:
            raise ValueError("an explicit model and API key are required")
        self.provider = cast(Literal["openai", "anthropic"], provider)
        self.model = model
        self.allow_insecure_http = allow_insecure_http
        self.wire_options = wire_options or WireOptions()
        if provider != "openai" and self.wire_options.thinking is not None:
            raise ValueError("explicit thinking control requires OpenAI-compatible transport")
        self.max_response_bytes = max_response_bytes
        validate_endpoint(base_url, allow_insecure_http)
        if max_response_bytes <= 0:
            raise ValueError("provider response limit must be positive")
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.max_tokens, self.timeout = max_tokens, timeout
        self.client = client

    def propose(self, context: Json, tools: tuple[ToolSpec, ...]) -> Reply | AssistantTurn:
        return self.invoke(self.prepare(context, tools))

    def prepare(self, context: Json, tools: tuple[ToolSpec, ...]) -> ModelRequest:
        """Pure assembly; the controller logs this before a transport can see it."""
        if self.provider == "openai":
            endpoint = "/chat/completions"
            headers = {"content-type": "application/json"}
            payload: Json = {
                "model": self.model, self.wire_options.token_parameter: self.max_tokens,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": canonical(context)}],
                "tools": [{"type": "function", "function": {
                    "name": t.name, "description": t.description, "parameters": t.parameters,
                }} for t in tools],
                "tool_choice": self.wire_options.tool_choice,
            }
            if self.wire_options.include_parallel_tool_calls:
                payload["parallel_tool_calls"] = False
            if self.wire_options.thinking is not None:
                payload["thinking"] = {"type": self.wire_options.thinking}
        else:
            endpoint = "/messages"
            headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
            payload = {
                "model": self.model, "max_tokens": self.max_tokens, "system": SYSTEM,
                "messages": [{"role": "user", "content": canonical(context)}],
                "tools": [{"name": t.name, "description": t.description,
                           "input_schema": t.parameters} for t in tools],
                "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
            }
        if self.wire_options.native_turns:
            system = SYSTEM.replace("and make one tool call at a time.",
                                    "and return text or a bounded batch of tool calls.")
            system += "Use finish as the only call in its turn. Text completion never promotes a kernel.\n"
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
            if self.wire_options.thinking is not None:
                payload["thinking"] = {"type": self.wire_options.thinking}
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
            data = request_json("POST", request.endpoint, headers,
                body=request.body_json.encode("utf-8"), timeout=request.timeout,
                max_bytes=self.max_response_bytes, client=self.client)
            raw_usage = data.get("usage", {})
            if isinstance(raw_usage, dict):
                usage = {key: value for key, value in raw_usage.items()
                         if key in {"input_tokens", "output_tokens", "prompt_tokens",
                                    "completion_tokens", "total_tokens", "cache_read_input_tokens",
                                    "cache_creation_input_tokens"}
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
