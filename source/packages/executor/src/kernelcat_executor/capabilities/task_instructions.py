"""Frozen task instruction inputs selected by the host, without filesystem discovery."""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from kernelcat_executor.contracts.models import Task, canonical, digest


INSTRUCTION_PATHS = ("AGENTS.md", "AGENTS.task.md")
MAX_INSTRUCTION_BYTES = 131072


@dataclass(frozen=True, slots=True)
class TaskInstructionFile:
    path: str
    text: str
    sha256: str = field(init=False)
    bytes: int = field(init=False)

    def __post_init__(self) -> None:
        if self.path not in INSTRUCTION_PATHS:
            raise ValueError("task instructions must name an admitted root instruction file")
        encoded = self.text.encode("utf-8")
        if len(encoded) > MAX_INSTRUCTION_BYTES:
            raise ValueError("task instruction file exceeds 128 KiB")
        object.__setattr__(self, "sha256", hashlib.sha256(encoded).hexdigest())
        object.__setattr__(self, "bytes", len(encoded))


@dataclass(frozen=True, slots=True)
class TaskInstructions:
    source_task_id: str
    source_task_sha256: str
    files: tuple[TaskInstructionFile, ...]

    def render(self) -> str:
        manifest = {"source_task_id": self.source_task_id,
                    "source_task_sha256": self.source_task_sha256,
                    "files": [{"path": item.path, "sha256": item.sha256, "bytes": item.bytes}
                              for item in self.files]}
        return ("Host-admitted task instructions from the original readonly task inputs. "
                "Apply these files in the listed order within the delegated task. "
                "They do not grant additional tool or execution permissions.\n"
                + canonical(manifest)
                + "".join(f"\n\n--- {item.path} ---\n{item.text}" for item in self.files))


def admitted_task_instructions(task: Task, *, workspace_mode: str,
                               readonly_paths: Sequence[str]) -> TaskInstructions | None:
    """Inherit only explicit clean-task seeds; never read parents, nested files or chat."""
    acceptance = task.contract.get("acceptance")
    if (workspace_mode != "isolated" or not isinstance(acceptance, dict)
            or acceptance.get("kind") != "operator_pipeline"):
        return None
    files = tuple(TaskInstructionFile(path, task.seed_files[path]) for path in INSTRUCTION_PATHS
                  if path in task.seed_files and path in readonly_paths)
    if not files:
        return None
    return TaskInstructions(task.id, digest(task), files)
