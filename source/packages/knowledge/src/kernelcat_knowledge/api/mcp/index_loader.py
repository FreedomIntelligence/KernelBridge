"""Compatibility wrapper over the protocol-independent active-revision repository."""
from __future__ import annotations

from pathlib import Path

from kernelcat_knowledge.api.bootstrap import get_application
from kernelcat_knowledge.api.mcp import config
from kernelcat_knowledge.generated.python_startup import (
    ExpectedPythonModules,
    admit_modules,
)
from kernelcat_knowledge.storage.filesystem import IndexLoader as _CoreIndexLoader
from kernelcat_knowledge.storage.filesystem import _normalize_path


class IndexLoader(_CoreIndexLoader):
    def __init__(
        self,
        static_path: Path | None = None,
        dynamic_path: Path | None = None,
        *,
        expected_modules: ExpectedPythonModules,
        card_path: Path | None = None,
        rule_path: Path | None = None,
        extra_paths: list[Path] | None = None,
    ) -> None:
        admit_modules(expected_modules, ("kernelcat_knowledge",))
        use_server_defaults = (
            static_path is None
            and dynamic_path is None
            and card_path is None
            and rule_path is None
            and not extra_paths
        )
        if use_server_defaults:
            super().__init__(
                static_path=config.CONFIG.static_index_path,
                dynamic_path=config.CONFIG.dynamic_index_path,
                card_path=config.CONFIG.card_index_path,
                rule_path=config.CONFIG.rule_index_path,
                repo_root=config.resource_root(),
            )
        else:
            super().__init__(
                static_path=static_path,
                dynamic_path=dynamic_path,
                card_path=card_path,
                rule_path=rule_path,
                extra_paths=extra_paths,
                repo_root=config.resource_root(),
            )


class LazyLoader:
    """Compatibility loader with explicit startup input, checked on every access."""

    def __init__(self, *, expected_modules: ExpectedPythonModules) -> None:
        from kernelcat_knowledge.generated.python_startup import normalize_expected

        self._expected_modules = normalize_expected(expected_modules, ("kernelcat_knowledge",))

    def __getattr__(self, name: str):
        app = get_application(config.resource_root(), expected_modules=self._expected_modules)
        return getattr(app.knowledge, name)


__all__ = ["IndexLoader", "LazyLoader", "_normalize_path"]
