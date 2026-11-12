"""Frozen instructions from admitted task seeds or an explicit project root."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
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
        return ("Host-admitted task instructions frozen from the selected workspace inputs. "
                "Apply these files in the listed order within the current task. "
                "They do not grant additional tool or execution permissions.\n"
                + canonical(manifest)
                + "".join(f"\n\n--- {item.path} ---\n{item.text}" for item in self.files))


def admitted_task_instructions(task: Task, *, workspace_mode: str,
                               readonly_paths: Sequence[str],
                               workspace_root: Path | None = None) -> TaskInstructions | None:
    """Select host-owned inputs, never parent chat or an ambient working directory.

    Isolated tasks inherit only readonly seeds. Ordinary projects use only the
    explicitly selected project root, with the same fixed filenames and byte
    bounds as the main session. No ancestor, nested or home discovery occurs.
    """
    if workspace_mode == "isolated":
        files = tuple(TaskInstructionFile(path, task.seed_files[path]) for path in INSTRUCTION_PATHS
                      if path in task.seed_files and path in readonly_paths)
    elif workspace_mode == "project" and workspace_root is not None:
        files = _project_instruction_files(workspace_root)
    else:
        return None
    if not files:
        return None
    return TaskInstructions(task.id, digest(task), files)


def _project_instruction_files(root: Path) -> tuple[TaskInstructionFile, ...]:
    """Read each regular file once through a bounded, no-symlink descriptor."""
    if not root.is_absolute():
        raise ValueError("instruction workspace must be an absolute directory")
    # The host may have selected /tmp or a project below a symlinked parent.
    # Canonicalize that explicit root; instruction files themselves cannot link out.
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("instruction workspace must be a directory")
    files = []
    for name in INSTRUCTION_PATHS:
        path = root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INSTRUCTION_BYTES:
            raise ValueError("invalid admitted instruction file: " + name)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            actual = os.fstat(stream.fileno())
            if (not stat.S_ISREG(actual.st_mode)
                    or (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino)):
                raise ValueError("instruction file changed during admission: " + name)
            data = stream.read(MAX_INSTRUCTION_BYTES + 1)
        if len(data) > MAX_INSTRUCTION_BYTES:
            raise ValueError("task instruction file exceeds 128 KiB")
        files.append(TaskInstructionFile(name, data.decode("utf-8")))
    return tuple(files)
