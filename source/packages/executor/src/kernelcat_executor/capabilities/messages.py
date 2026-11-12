"""Pure text/tool turn codecs. No HTTP, execution, hidden retries or ledger writes."""
from __future__ import annotations

import json
from typing import Literal, cast

from kernelcat_executor.contracts.messages import AssistantTurn, TextContent, ThinkingContent, ToolCall
from kernelcat_executor.contracts.models import Json, ToolFailure, canonical


def parse_turn(provider: Literal["openai", "anthropic"], data: Json, usage: Json) -> AssistantTurn:
    blocks: list[TextContent | ToolCall | ThinkingContent] = []
    if provider == "openai":
        choices = data["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("expected one assistant choice")
        choice = choices[0]
        reason = choice.get("finish_reason")
        if reason not in {None, "stop", "tool_calls"}:
            raise ToolFailure("provider output was truncated or filtered",
                              details={"usage": usage, "category": "incomplete_output"})
        message = choice["message"]
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            blocks.append(ThinkingContent(thinking=reasoning))
        text = message.get("content")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("unsupported assistant content")
            if text:
                blocks.append(TextContent(text=text))
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls must be an array")
        for call in raw_calls:
            if call.get("type") != "function":
                raise ValueError("unsupported tool call type")
            function = call["function"]
            arguments = json.loads(function["arguments"])
            blocks.append(ToolCall(id=call["id"], name=function["name"], arguments=arguments))
        if reason is not None and (reason == "tool_calls") != bool(raw_calls):
            raise ValueError("stop reason and tool calls disagree")
    else:
        reason = data.get("stop_reason")
        if reason not in {None, "end_turn", "tool_use"}:
            raise ToolFailure("provider output was truncated or filtered",
                              details={"usage": usage, "category": "incomplete_output"})
        content = data["content"]
        if not isinstance(content, list):
            raise ValueError("content must be an array")
        for block in content:
            if block["type"] == "text":
                blocks.append(TextContent(text=block["text"]))
            elif block["type"] == "tool_use":
                blocks.append(ToolCall(id=block["id"], name=block["name"], arguments=block["input"]))
            else:
                # Never silently discard signed thinking or unhandled content.
                raise ToolFailure("unsupported provider content block",
                                  details={"usage": usage, "category": "unsupported_content"})
        has_calls = any(isinstance(block, ToolCall) for block in blocks)
        if reason is not None and (reason == "tool_use") != has_calls:
            raise ValueError("stop reason and tool calls disagree")
    has_calls = any(isinstance(block, ToolCall) for block in blocks)
    return AssistantTurn(content=tuple(blocks), stop_reason="tool_calls" if has_calls else "stop",
                         usage=usage)


def conversation(provider: Literal["openai", "anthropic"], context: Json) -> list[Json]:
    """Encode whole exchanges; no bare tool result or incomplete call batch is emitted."""
    groups = context.get("turns", [])
    if not isinstance(groups, list):
        raise ValueError("turns must be an array")
    # Task/policy/budget stay explicit. Legacy history is preserved on mixed routes.
    anchor = {key: value for key, value in context.items() if key not in {"turns", "inputs"}}
    messages: list[Json] = [{"role": "user", "content": canonical(anchor)}]
    inputs = list(context.get("inputs", []))
    used_ids: set[str] = set()

    def emit_inputs(before_step: int | None) -> None:
        while inputs and (before_step is None or inputs[0]["step"] <= before_step):
            for message in inputs.pop(0)["messages"]:
                messages.append({"role": "user", "content": message["text"]})

    for group in groups:
        step = group.get("step")
        if step is not None:
            emit_inputs(step)
        turn = AssistantTurn.model_validate_json(canonical(group["assistant"]))
        results = group["results"]
        if not isinstance(results, list) or len(results) != len(turn.tool_calls):
            raise ValueError("incomplete tool-result group")
        for call, result in zip(turn.tool_calls, results, strict=True):
            if result["tool_call_id"] != call.id or result["tool_name"] != call.name:
                raise ValueError("tool-result identity mismatch")
            if result["status"] not in {"completed", "failed"}:
                raise ValueError("unresolved tool result")
        # Providers sometimes reuse a call ID on a later turn. Rename only the wire
        # alias of that entire exchange; durable IDs and execution IDs are unchanged.
        aliases: dict[str, str] = {}
        for call in turn.tool_calls:
            alias = call.id
            suffix = 0
            while alias in used_ids:
                suffix += 1
                alias = f"kc_{len(messages)}_{suffix}"
            used_ids.add(alias)
            aliases[call.id] = alias
        if provider == "openai":
            assistant: Json = {"role": "assistant", "content": turn.text or None}
            if turn.reasoning:
                assistant["reasoning_content"] = turn.reasoning
            if turn.tool_calls:
                assistant["tool_calls"] = [{"id": aliases[c.id], "type": "function",
                    "function": {"name": c.name, "arguments": canonical(c.arguments)}}
                    for c in turn.tool_calls]
            messages.append(assistant)
            for result in results:
                messages.append({"role": "tool", "tool_call_id": aliases[result["tool_call_id"]],
                                 "content": canonical(result)})
        else:
            content: list[Json] = []
            for block in turn.content:
                if isinstance(block, ThinkingContent):
                    continue
                if isinstance(block, TextContent):
                    content.append({"type": "text", "text": block.text})
                else:
                    content.append({"type": "tool_use", "id": aliases[block.id],
                                    "name": block.name, "input": block.arguments})
            messages.append({"role": "assistant", "content": content})
            if results:
                messages.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": aliases[r["tool_call_id"]],
                    "content": canonical(r), "is_error": r["status"] == "failed",
                } for r in results]})
    emit_inputs(None)
    # Detach nested dictionaries returned by caller-supplied projections.
    return cast(list[Json], json.loads(canonical(current_turn_reasoning(messages))))


def current_turn_reasoning(messages: list[Json]) -> list[Json]:
    """Keep reasoning_content only after the last user message: thinking mode needs it
    back within the turn's tool loop; earlier turns' reasoning only costs tokens."""
    last_user = max((index for index, item in enumerate(messages) if item.get("role") == "user"), default=-1)
    for index, item in enumerate(messages):
        if index < last_user and item.get("role") == "assistant":
            item.pop("reasoning_content", None)
    return messages


def harness_conversation(provider: Literal["openai", "anthropic"],
                         messages: list[Json]) -> list[Json]:
    """Encode the validated text/tool projection without rebuilding business input."""
    encoded: list[Json] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if role == "toolResult":
            _harness_result(provider, message, blocks, encoded)
        elif provider == "openai" and role == "assistant":
            assistant: Json = {"role": "assistant", "content": "".join(
                block["text"] for block in blocks if block["type"] == "text") or None}
            reasoning = "".join(block["thinking"] for block in blocks if block["type"] == "thinking")
            if reasoning:
                assistant["reasoning_content"] = reasoning
            calls = [block for block in blocks if block["type"] == "toolCall"]
            if calls:
                assistant["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": canonical(call["arguments"]),
                }} for call in calls]
            encoded.append(assistant)
        elif provider == "anthropic" and role == "assistant":
            encoded.append({"role": role, "content": [
                {"type": "tool_use", "id": block["id"], "name": block["name"],
                 "input": block["arguments"]} if block["type"] == "toolCall" else block
                for block in blocks if block["type"] != "thinking"]})
        else:
            encoded.append({"role": role, "content": content})
    return cast(list[Json], json.loads(canonical(current_turn_reasoning(encoded))))


def _harness_result(provider: Literal["openai", "anthropic"], message: Json,
                    blocks: list[Json], encoded: list[Json]) -> None:
    if provider == "openai":
        encoded.append({"role": "tool", "tool_call_id": message["toolCallId"],
                        "content": "\n".join(block["text"] for block in blocks) or "(no tool output)"})
        return
    result = {"type": "tool_result", "tool_use_id": message["toolCallId"],
              "content": blocks, "is_error": message["isError"]}
    # One assistant batch can yield several serial SDK result messages.
    if encoded and encoded[-1]["role"] == "user" and isinstance(encoded[-1]["content"], list):
        encoded[-1]["content"].append(result)
    else:
        encoded.append({"role": "user", "content": [result]})
