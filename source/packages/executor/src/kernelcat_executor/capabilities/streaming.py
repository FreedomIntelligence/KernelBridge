"""Optional bounded SSE transport; final replies still settle through Gateway.

Observers carry presentation deltas only. They cannot authorize tools, settle
usage, retry a request or change the admitted wire body.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json

import httpx

from kernelcat_executor.capabilities.http import _status_failure
from kernelcat_executor.contracts.models import ToolFailure

observer = ContextVar("kernelbridge_model_stream", default=None)


@contextmanager
def observe(callback):
    token = observer.set(callback)
    try:
        yield
    finally:
        observer.reset(token)


def prepare_stream(payload, provider):
    if observer.get() is not None:
        payload["stream"] = True
        if provider == "openai":
            payload["stream_options"] = {"include_usage": True}


def _secret_prefix_suffix(text, secret):
    """Length of the longest suffix of text that is a proper prefix of secret."""
    if not secret:
        return 0
    for length in range(min(len(text), len(secret) - 1), 0, -1):
        if text.endswith(secret[:length]):
            return length
    return 0


def request_stream(request, headers, *, provider, max_bytes, client=None, secret=""):
    callback = observer.get()
    text, reasoning, calls, blocks, usage = "", "", {}, {}, {}
    # Whether any event arrived: after that the TUI has shown output, so a failure
    # is not repeated automatically (a retry would stream it again).
    started = False
    reason = None
    ended = False

    pending = {"text_delta": "", "thinking_delta": ""}

    def emit(kind, delta, final=False):
        pending[kind] += delta
        if secret and secret in pending[kind]:
            raise ToolFailure("provider returned authentication material", details={"category": "credential_echo"})
        # Hold only a suffix that could still become the credential. Emitted
        # text never contains a full secret, and ordinary prose is not delayed.
        size = len(pending[kind]) if final else len(pending[kind]) - _secret_prefix_suffix(pending[kind], secret)
        if size and callback:
            callback({"type": kind, "delta": pending[kind][:size]})
        pending[kind] = pending[kind][size:]

    def emit_text(delta, final=False):
        emit("text_delta", delta, final)

    def consume(value):
        nonlocal text, reasoning, reason, ended, started
        started = True
        if provider == "openai":
            usage.update(value.get("usage") or {})
            choices = value.get("choices", [])
            if len(choices) > 1:
                raise ValueError("multiple streaming choices")
            for choice in choices:
                if choice.get("index", 0) != 0:
                    raise ValueError("unexpected streaming choice")
                delta = choice.get("delta", {})
                # Reasoning streams to the TUI and is kept in the settled reply: in
                # thinking mode with tool calls the provider requires it back.
                thinking = delta.get("reasoning_content") or ""
                if thinking:
                    reasoning += thinking
                    emit("thinking_delta", thinking)
                piece = delta.get("content") or ""
                text += piece
                emit_text(piece)
                for call in delta.get("tool_calls", []):
                    index = call["index"]
                    current = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    current["id"] += call.get("id", "")
                    for key in ("name", "arguments"):
                        current["function"][key] += call.get("function", {}).get(key, "")
                if choice.get("finish_reason") is not None:
                    reason = choice["finish_reason"]
        else:
            kind = value["type"]
            if kind == "message_start":
                usage.update(value["message"].get("usage", {}))
            elif kind == "content_block_start":
                block = value["content_block"]
                if block["type"] not in {"text", "tool_use"}:
                    raise ValueError("unsupported streaming content")
                blocks[value["index"]] = {**block, "partial_json": ""}
            elif kind == "content_block_delta":
                block, delta = blocks[value["index"]], value["delta"]
                if delta["type"] == "text_delta":
                    block["text"] += delta["text"]
                    text += delta["text"]
                    emit_text(delta["text"])
                elif delta["type"] == "input_json_delta":
                    block["partial_json"] += delta["partial_json"]
                else:
                    raise ValueError("unsupported streaming delta")
            elif kind == "message_delta":
                reason = value["delta"].get("stop_reason", reason)
                usage.update(value.get("usage", {}))
            elif kind == "message_stop":
                ended = True
            elif kind == "error":
                raise ToolFailure("provider streaming error", details={"category": "protocol"})

    def send(session):
        nonlocal ended
        with session.stream("POST", request.endpoint, headers=headers, content=request.body_json.encode(),
                            timeout=request.timeout, follow_redirects=False) as response:
            if not 200 <= response.status_code < 300:
                raise _status_failure(response.status_code, response.headers.get("retry-after"))
            total, buffer, data = 0, b"", []
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ToolFailure("provider stream exceeded byte limit", details={"category": "response_limit"})
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.rstrip(b"\r")
                    if line.startswith(b"data:"):
                        data.append(line[5:].lstrip(b" "))
                    elif not line and data:
                        encoded = b"\n".join(data)
                        data.clear()
                        if encoded == b"[DONE]":
                            ended = True
                        else:
                            consume(json.loads(encoded))
            if buffer.strip() or data or not ended or reason is None:
                raise ToolFailure("truncated provider stream", details={"category": "incomplete_output"})

    try:
        if client is not None:
            send(client)
        else:
            with httpx.Client(follow_redirects=False, trust_env=False) as session:
                send(session)
        emit("thinking_delta", "", final=True)
        emit_text("", final=True)
    except httpx.HTTPError as error:
        raise ToolFailure("provider streaming transport failure", details={
            "category": "timeout" if isinstance(error, httpx.TimeoutException) else "transport", "automatic_retry": False,
            "output_started": started}) from None
    if provider == "openai":
        message = {"content": text, "tool_calls": [calls[k] for k in sorted(calls)]}
        if reasoning:
            message["reasoning_content"] = reasoning
        return {"choices": [{"finish_reason": reason, "message": message}], "usage": usage}
    content = []
    for key in sorted(blocks):
        block = blocks[key]
        partial = block.pop("partial_json")
        if block["type"] == "tool_use" and partial:
            block["input"] = json.loads(partial)
        content.append(block)
    return {"content": content, "stop_reason": reason, "usage": usage}
