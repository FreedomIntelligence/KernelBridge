"""Explicit capability registration: no plugin loader or arbitrary shell."""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from kernelcat_executor.contracts.models import Action, Json, Outcome, ToolSpec
from kernelcat_executor.contracts.ports import SkillLibrary as Skills
from kernelcat_executor.contracts.ports import SourceWorkspace as Workspace


def schema(properties: Json, required: list[str]) -> Json:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


STRING: Json = {"type": "string"}
PAGE: Json = {"offset": {"type": "integer", "minimum": 0,
                        "description": "Zero-based Unicode code-point offset; use returned next_offset to continue."},
              "limit": {"type": "integer", "minimum": 1, "maximum": 8192,
                        "description": "Maximum Unicode code points requested. A bounded page may contain fewer; follow next_offset."}}


class FunctionTool:
    def __init__(self, spec: ToolSpec, function: Callable[[Action], Outcome], *,
                 execution_mode: Literal["sequential", "parallel"] = "sequential") -> None:
        self.spec, self.function = spec, function
        self.execution_mode = execution_mode

    def execute(self, action: Action) -> Outcome:
        return self.function(action)


def builtin_tools(workspace: Workspace, skills: Skills) -> list[FunctionTool]:
    def load(action: Action) -> Outcome:
        return Outcome(data=skills.load(action.arguments["name"],
            action.arguments.get("resource", "SKILL.md"), action.arguments.get("offset", 0),
            action.arguments.get("limit", 8192)))
    return [
        FunctionTool(ToolSpec(name="load_skill", description="Read a skill/reference; paginate next_offset.",
            parameters=schema({"name": STRING, "resource": STRING, **PAGE}, ["name"]), credits=1), load),
        FunctionTool(ToolSpec(name="read_file", description="Read source with whole-file SHA. offset, limit and next_offset count Unicode code points, not bytes or lines. Use a project-relative path, never an absolute path. Follow returned next_offset; null means end of file.",
            parameters=schema({"path": STRING, **PAGE}, ["path"]), credits=1), workspace.read),
        FunctionTool(ToolSpec(name="list_files", description="Discover project files recursively (relative paths). Bounded, excludes caches/symlinks; narrow path/glob pattern if truncated.",
            parameters=schema({"path": STRING, "pattern": STRING,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, []), credits=1), workspace.list_files),
        FunctionTool(ToolSpec(name="search_text", description="Search literal text in project files with line numbers. Case-insensitive by default. Bounded; narrow path/glob pattern if truncated.",
            parameters=schema({"query": {"type": "string", "minLength": 1, "maxLength": 1024},
                "path": STRING, "pattern": STRING, "ignore_case": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]), credits=1), workspace.search_text),
        FunctionTool(ToolSpec(name="write_file", description="Write full file; expected digest null only for new.",
            parameters=schema({"path": STRING, "text": STRING,
                "expected_sha256": {"type": ["string", "null"]}},
                ["path", "text", "expected_sha256"]), credits=1), workspace.write),
        FunctionTool(ToolSpec(name="edit_file", description="Replace one unique exact match; verify old-file SHA.",
            parameters=schema({"path": STRING, "old_text": {"type": "string", "minLength": 1},
                "new_text": STRING, "expected_sha256": STRING},
                ["path", "old_text", "new_text", "expected_sha256"]), credits=1), workspace.edit),
        FunctionTool(ToolSpec(name="finish", description="Finish analysis; never promotes a kernel.",
            parameters=schema({"summary": STRING}, ["summary"]), credits=0),
            lambda a: Outcome(data={"summary": a.arguments["summary"], "promoted": False})),
    ]
