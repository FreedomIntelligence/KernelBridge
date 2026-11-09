"""Finite, read-only Knowledge tools bound by trusted Worker composition."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from kernelcat_executor.capabilities.workspace_tools import FunctionTool, schema
from kernelcat_executor.contracts.models import Action, Json, Outcome, ToolFailure, ToolSpec
from kernelcat_executor.contracts.ports import Tool
from kernelcat_knowledge.contracts import ContractError


class KnowledgeReadSurface(Protocol):
    """The existing live and retained owners expose these four read operations."""

    def search(self, request: Mapping[str, Any]) -> Json: ...
    def show(self, knowledge_id: str, *, view: str = "agent", max_chars: int = 12000) -> Json: ...
    def source_show(self, source_id: str) -> Json: ...
    def raw_read(self, *, evidence_id: str) -> Json: ...


def _call(operation: str, call: Callable[[], Json]) -> Json:
    try:
        return call()
    except ContractError as error:
        raise ToolFailure(
            f"{operation}: {error}", details={"code": "INVALID_CONTRACT"}
        ) from error


def _read(operation: str, call: Callable[[], Json]) -> Outcome:
    result = _call(operation, call)
    return Outcome(status="completed" if result["status"] == "ok" else "failed", data=result)


def _search(application: KnowledgeReadSurface, request: Mapping[str, Any]) -> Json:
    try:
        return application.search(request)
    except (ValueError, TypeError) as error:
        # The current owner also raises these while normalizing numeric query fields.
        raise ToolFailure(
            f"knowledge_search: {error}", details={"code": "INVALID_CONTRACT"}
        ) from error


def knowledge_tools(application: KnowledgeReadSurface) -> list[Tool]:
    """Expose only owner-validated reads; every call retains ordinary Gateway admission."""
    identifier = {"type": "string", "minLength": 1, "maxLength": 512}

    def search(action: Action) -> Outcome:
        result = _call(
            "knowledge_search", lambda: _search(application, action.arguments["request"])
        )
        return Outcome(status="completed", data=result)

    def show(action: Action) -> Outcome:
        return _read("knowledge_show", lambda: application.show(
            action.arguments["knowledge_id"],
            view=action.arguments.get("view", "agent"),
            max_chars=action.arguments.get("max_chars", 12000),
        ))

    def source(action: Action) -> Outcome:
        return _read("knowledge_source", lambda: application.source_show(
            action.arguments["source_id"]
        ))

    def read(action: Action) -> Outcome:
        return _read("knowledge_read", lambda: application.raw_read(
            evidence_id=action.arguments["evidence_id"]
        ))

    return [
        FunctionTool(ToolSpec(
            name="knowledge_search",
            description=(
                "Search the configured Knowledge root using a structured "
                "kernelcat.knowledge-query/v1 request. Knowledge validates its scope, "
                "limit (1–10) and token_budget (256–1200). The V2 result retains evidence "
                "maturity and the corpus/config/schema binding of its query receipt."
            ),
            parameters=schema({"request": {
                "type": "object",
                "description": (
                    "Opaque KnowledgeQuery owned and validated by Knowledge. Supply its "
                    "schema_version, one operator_id or mechanism_family, phase, bottleneck "
                    "and decision_question, with optional owner-supported scope predicates."
                ),
            }}, ["request"]),
            credits=1,
        ), search),
        FunctionTool(ToolSpec(
            name="knowledge_show",
            description=(
                "Read a logical Knowledge page or exact revision by its registered ID. "
                "The human view bounds page text with max_chars; agent and audit retain "
                "the owner's structured metadata and evidence state."
            ),
            parameters=schema({
                "knowledge_id": {**identifier, "description": "Logical page or exact revision ID."},
                "view": {"type": "string", "enum": ["agent", "audit", "human"]},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000},
            }, ["knowledge_id"]),
            credits=1,
        ), show),
        FunctionTool(ToolSpec(
            name="knowledge_source",
            description=(
                "Read one registered source identity from the configured Knowledge root. "
                "Stored identity and authority scopes do not establish current device results."
            ),
            parameters=schema({"source_id": {
                **identifier, "description": "Registered source identity."}
            }, ["source_id"]),
            credits=1,
        ), source),
        FunctionTool(ToolSpec(
            name="knowledge_read",
            description=(
                "Read verified source content through one registered evidence ID. "
                "Knowledge rechecks source bytes and the bound selector, and applies its "
                "line/byte limits. Arbitrary paths and selector overrides are unavailable."
            ),
            parameters=schema({"evidence_id": {
                **identifier, "description": "Registered evidence selector identity."}
            }, ["evidence_id"]),
            credits=1,
        ), read),
    ]
