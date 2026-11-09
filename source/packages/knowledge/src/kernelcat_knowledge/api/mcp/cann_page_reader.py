"""Single-ID cann-ask adapter over the shared page-read application service."""
from __future__ import annotations

from kernelcat_knowledge.api.mcp.index_loader import IndexLoader, LazyLoader
from kernelcat_knowledge.api.mcp.page_reader import PageReader
from kernelcat_knowledge.generated.python_startup import (
    ExpectedPythonModules,
    admit_modules,
)
from kernelcat_knowledge.ports.errors import ApplicationError
from kernelcat_knowledge.query.read import show_page


class CannPageReader:
    def __init__(
        self, loader: IndexLoader | None = None, *, expected_modules: ExpectedPythonModules,
    ) -> None:
        admit_modules(expected_modules, ("kernelcat_knowledge",))
        self.loader = loader if loader is not None else LazyLoader(expected_modules=expected_modules)
        self.reader = PageReader(self.loader)

    def get_page(self, knowledge_id: str) -> dict:
        try:
            page = show_page(knowledge_id, reader=self.reader)
        except ApplicationError as exc:
            return {"id": knowledge_id, "error": exc.message}
        return {
            "id": knowledge_id,
            "frontmatter": page.get("frontmatter") or {},
            "content": page.get("content") or "",
            "qValue": float(page.get("qValue") or 0.0),
        }

    def get_index(self) -> dict:
        return self.reader.get_index()
