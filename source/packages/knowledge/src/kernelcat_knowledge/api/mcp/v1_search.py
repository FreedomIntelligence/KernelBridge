"""Compatibility wrapper for the shared KnowledgeQuery application service."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernelcat_knowledge.api.bootstrap import build_application
from kernelcat_knowledge.api.mcp import config
from kernelcat_knowledge.api.mcp.index_loader import IndexLoader
from kernelcat_knowledge.generated.python_startup import ExpectedPythonModules


def search_v1(
    request: Mapping[str, Any],
    *,
    expected_modules: ExpectedPythonModules,
    loader: IndexLoader | None = None,
) -> dict[str, Any]:
    return build_application(
        config.resource_root(),
        expected_modules=expected_modules,
        knowledge=loader,
    ).search(request)


__all__ = ["search_v1"]
