"""Validate effective Pi input without selecting context or granting capabilities."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol, cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from kernelcat_executor.contracts.context import validate_context_source
from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.models import Conflict, Json, ToolSpec, canonical
from kernelcat_executor.contracts.runtime import SCHEMA_REGISTRY
from kernelcat_executor.generated.harness import (
    SCHEMA,
    HarnessInput,
    HarnessModelRequest,
    NativeBeginRequest,
    NativeCallRequest,
    NativeSettleRequest,
)

PI_READ_TOOLS = frozenset({"read", "ls", "find", "grep"})
PI_WRITE_TOOLS = frozenset({"write", "edit", "bash"})
NATIVE_TOOLS = frozenset({"codemode", "tool_search"})
MCP_PREFIX = "mcp__kernelcat__"
CONTEXT_PREFIX = "KERNELCAT_CONTEXT_V1\n"
CONTEXT_SUFFIX = "\nEND_KERNELCAT_CONTEXT_V1\n"
_REGISTRY = SCHEMA_REGISTRY


def _validate(value: object, name: str) -> object:
    validator = Draft202012Validator(
        {**SCHEMA, "$ref": "#/$defs/" + name}, registry=_REGISTRY)
    return validated_snapshot(value, validator, name)


def validate_harness_request(value: object) -> HarnessModelRequest:
    return cast(HarnessModelRequest, _validate(value, "HarnessModelRequest"))


def validate_native_begin(value: object) -> NativeBeginRequest:
    return cast(NativeBeginRequest, _validate(value, "NativeBeginRequest"))


def validate_native_call(value: object) -> NativeCallRequest:
    return cast(NativeCallRequest, _validate(value, "NativeCallRequest"))


def validate_native_settle(value: object) -> NativeSettleRequest:
    return cast(NativeSettleRequest, _validate(value, "NativeSettleRequest"))


def validate_replay(value: object, definition: str) -> Json:
    return cast(Json, _validate(value, definition))


class NativeReplay(Protocol):
    """Finite immutable-file validation; Pi owns the session tree and continuation."""

    def start(self, latest: Json | None) -> Json: ...
    def checkpoint(self, value: object) -> Json: ...


def native_name(name: str, allowed: frozenset[str]) -> bool:
    return (name in NATIVE_TOOLS
            or (name in PI_READ_TOOLS and "read_file" in allowed)
            or (name in PI_WRITE_TOOLS and "write_file" in allowed)) or (
        name.startswith(MCP_PREFIX) and name[len(MCP_PREFIX):] in allowed)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate protected context key")
        result[key] = value
    return result


def _protected_context(prompt: str, context: Json) -> None:
    if not prompt.startswith(CONTEXT_PREFIX):
        raise Conflict("effective Pi input has no protected ContextSource prefix")
    encoded, separator, _ = prompt[len(CONTEXT_PREFIX):].partition(CONTEXT_SUFFIX)
    if not separator:
        raise Conflict("effective Pi input has an unterminated ContextSource prefix")
    decoded = json.loads(encoded, object_pairs_hook=_unique_object)
    supplied = validate_context_source(decoded)
    expected = validate_context_source(context)
    if canonical(supplied) != canonical(expected):
        raise Conflict("effective Pi input changed the accepted ContextSource")


def _text_blocks(content: object, *, calls: bool) -> list[Json]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise ValueError("Pi message content must be text or a block array")
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Pi content block must be an object")
        if block.get("type") == "text":
            if set(block) != {"type", "text"} or not isinstance(block["text"], str):
                raise ValueError("unsupported Pi text block metadata")
        elif calls and block.get("type") == "toolCall":
            _tool_call_block(block)
        elif calls and block.get("type") == "thinking":
            # Provider reasoning on an assistant message; returned within the same turn.
            if set(block) != {"type", "thinking"} or not isinstance(block["thinking"], str):
                raise ValueError("unsupported Pi thinking block metadata")
        else:
            raise ValueError("unsupported Pi content block")
    return content


def _tool_call_block(block: Json) -> None:
    if set(block) != {"type", "id", "name", "arguments"}:
        raise ValueError("unsupported Pi tool call metadata")
    for field in ("id", "name"):
        if not isinstance(block[field], str) or not 0 < len(block[field]) <= 256:
            raise ValueError(f"invalid Pi tool call {field}")
    if not isinstance(block["arguments"], dict):
        raise ValueError("Pi tool arguments must be an object")


def _messages(messages: list[Json]) -> None:
    pending: dict[str, str] = {}
    used: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "toolResult":
            _tool_result(message, pending)
            continue
        if role not in {"user", "assistant"} or set(message) != {"role", "content"}:
            raise ValueError("unsupported Pi message role or diagnostic metadata")
        if pending:
            raise ValueError("Pi conversation contains unresolved tool calls")
        for block in _text_blocks(message["content"], calls=role == "assistant"):
            if block["type"] == "toolCall":
                if block["id"] in used:
                    raise ValueError("Pi conversation reuses a tool-call identity")
                used.add(block["id"])
                pending[block["id"]] = block["name"]
    if pending:
        raise ValueError("Pi conversation ends with unresolved tool calls")


def _tool_result(message: Json, pending: dict[str, str]) -> None:
    if set(message) != {"role", "toolCallId", "toolName", "content", "isError"}:
        raise ValueError("unsupported Pi tool result metadata")
    if type(message["isError"]) is not bool:
        raise ValueError("Pi tool result isError must be boolean")
    call_id, name = message["toolCallId"], message["toolName"]
    if not isinstance(call_id, str) or pending.get(call_id) != name:
        raise ValueError("Pi tool result has no matching tool call")
    _text_blocks(message["content"], calls=False)
    del pending[call_id]


def _local_schema(schema: Json) -> None:
    Draft202012Validator.check_schema(schema)
    resource: Resource[object] = Resource.from_contents(schema, default_specification=DRAFT202012)
    pending = [(resource, Registry[object]().resolver_with_root(resource))]
    visited: set[int] = set()
    while pending:
        current, resolver = pending.pop()
        if id(current.contents) in visited:
            continue
        visited.add(id(current.contents))
        if isinstance(current.contents, dict):
            for keyword in ("$ref", "$dynamicRef"):
                reference = current.contents.get(keyword)
                if reference is None:
                    continue
                if not isinstance(reference, str) or not reference.startswith("#"):
                    raise ValueError("effective tool schemas cannot resolve external references")
                resolved = resolver.lookup(reference)
                target = Resource.from_contents(resolved.contents, default_specification=DRAFT202012)
                pending.append((target, resolved.resolver))
        pending.extend((child, resolver.in_subresource(child)) for child in current.subresources())


def validate_harness(value: object, context: Json, specs: tuple[ToolSpec, ...],
                     maximum_bytes: int) -> HarnessInput:
    """Pinned Node metadata is trusted; current Gateway definitions remain authoritative."""
    checked = cast(HarnessInput, _validate(value, "HarnessInput"))
    if len(canonical(checked).encode("utf-8")) > maximum_bytes:
        raise ValueError("effective Pi input exceeds context_bytes")
    _protected_context(checked["system_prompt"], context)
    _messages(cast(list[Json], checked["messages"]))
    known = {spec.name: spec for spec in specs}
    leaves = frozenset(known) - {"delegate", "finish"}
    seen: set[str] = set()
    for tool in checked["tools"]:
        name = tool["name"]
        if name in seen:
            raise ValueError("effective Pi input repeats a tool declaration")
        seen.add(name)
        if name in known:
            expected = {key: value for key, value in known[name].model_dump(mode="json").items()
                        if key != "credits"}
            if canonical(tool) != canonical(expected):
                raise Conflict(f"effective Pi tool differs from Gateway specification: {name}")
        elif not native_name(name, leaves):
            raise Conflict(f"effective Pi input declares an unauthorized tool: {name}")
        _local_schema(cast(Json, tool["parameters"]))
    return checked


def validate_native_arguments(name: str, arguments: Json,
                              declarations: Mapping[str, Json]) -> None:
    declaration = declarations.get(name)
    if declaration is None:
        raise Conflict(f"native tool was not declared in the admitted model input: {name}")
    if not Draft202012Validator(declaration["parameters"], registry=Registry()).is_valid(arguments):
        raise Conflict(f"native tool arguments do not match its admitted declaration: {name}")


def validate_native_result(result: Json, status: str) -> None:
    if (set(result) - {"content", "structuredContent", "isError"}
            or "content" not in result or type(result.get("isError")) is not bool):
        raise ValueError("native result requires supported content and explicit isError")
    _text_blocks(result["content"], calls=False)
    if status != "unknown" and result["isError"] != (status == "failed"):
        raise Conflict("native settlement status differs from its result")
