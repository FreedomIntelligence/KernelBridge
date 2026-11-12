"""Automatic retry of transient provider failures (Claude Code / Codex practice).

A model call has no side effects, so a failed attempt may be repeated; tool calls
are never retried here. Each attempt is its own admitted, metered Journal action on
the deterministic successor chain (recovery._successor), so replay and resume see
every attempt and never repeat one. Retry only what failed before any output:
HTTP 429 and 5xx (including 529), and connection failures or timeouts before the
stream started. Authentication, other 4xx, protocol, truncated or limit failures,
and unknown outcomes are never retried.
"""
from __future__ import annotations

import random
from typing import Callable

from kernelcat_executor.contracts.models import Json

BASE_SECONDS = 1.0
CAP_SECONDS = 20.0
RETRY_AFTER_CAP_SECONDS = 60.0


def retryable(details: Json) -> bool:
    category = details.get("category") if isinstance(details, dict) else None
    if category in {"rate_limit", "server"}:
        return True
    return category in {"timeout", "transport"} and details.get("output_started") is False


def retry_delay(details: Json, attempt: int, max_retries: int,
                rng: Callable[[], float] = random.random) -> float | None:
    """Seconds to wait before retry number attempt+1, or None to stop retrying.

    Capped exponential backoff with equal jitter; Retry-After is a floor (capped)."""
    if attempt >= max_retries or not retryable(details):
        return None
    window = min(CAP_SECONDS, BASE_SECONDS * 2 ** attempt)
    delay = window / 2 + rng() * window / 2
    after = details.get("retry_after")
    if isinstance(after, (int, float)) and after > 0:
        delay = max(delay, min(RETRY_AFTER_CAP_SECONDS, float(after)))
    return round(delay, 3)


def failure_summary(details: Json) -> Json:
    """The part of a failure the user sees: category and HTTP status, never a body."""
    return {key: details[key] for key in ("category", "http_status") if isinstance(details, dict) and key in details}
