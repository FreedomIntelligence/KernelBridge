"""Bounded, non-redirecting provider HTTP with deliberately redacted failures."""
from __future__ import annotations

import json
from typing import cast

import httpx

from kernelcat_executor.contracts.models import Json, ToolFailure, canonical


def _status_failure(status: int) -> ToolFailure:
    category = ("authentication" if status in {401, 403} else
                "rate_limit" if status == 429 else
                "redirect" if 300 <= status < 400 else
                "server" if status >= 500 else "http")
    return ToolFailure(f"provider HTTP {status}", details={
        "category": category, "http_status": status, "automatic_retry": False})


def request_json(method: str, endpoint: str, headers: dict[str, str], *,
                 body: bytes | None = None, timeout: float = 120.0,
                 max_bytes: int = 1048576, client: httpx.Client | None = None) -> Json:
    """Never follow redirects, including when an injected client defaults to doing so.

    Caps the captured decoded response. This is not a bound on every allocation
    inside the HTTP/decompression library. No response body or auth header is
    included in raised failures; every call is one attempt with no hidden retry.
    """
    if max_bytes <= 0 or timeout <= 0:
        raise ValueError("provider limits must be positive")

    def send(session: httpx.Client) -> Json:
        with session.stream(method, endpoint, headers=headers, content=body,
                            timeout=timeout, follow_redirects=False) as response:
            if not 200 <= response.status_code < 300:
                raise _status_failure(response.status_code)
            output = bytearray()
            for chunk in response.iter_bytes(chunk_size=16384):
                if len(output) + len(chunk) > max_bytes:
                    raise ToolFailure("provider response exceeded its byte limit",
                                      details={"category": "response_limit", "limit_bytes": max_bytes})
                output.extend(chunk)
        try:
            parsed = json.loads(output)
            if not isinstance(parsed, dict):
                raise ValueError("expected object")
            canonical(parsed)
            return cast(Json, parsed)
        except (ValueError, TypeError, RecursionError):
            raise ToolFailure("malformed provider response", details={"category": "protocol"}) from None

    try:
        if client is not None:
            return send(client)
        with httpx.Client(follow_redirects=False, trust_env=False) as session:
            return send(session)
    except httpx.HTTPError as error:
        category = "timeout" if isinstance(error, httpx.TimeoutException) else "transport"
        raise ToolFailure(f"provider transport failure: {type(error).__name__}",
                          details={"category": category, "automatic_retry": False}) from None
