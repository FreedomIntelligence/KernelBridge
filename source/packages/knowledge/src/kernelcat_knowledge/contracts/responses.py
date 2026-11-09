"""Canonical envelopes shared by the existing Knowledge operations."""

from __future__ import annotations

from typing import Any

from kernelcat_knowledge.contracts.canonical import sha256_digest


def response(schema_version: str, status: str, **payload: Any) -> dict[str, Any]:
    result = {"schema_version": schema_version, "status": status, **payload}
    result["digest"] = sha256_digest(result)
    return result
