"""Immutable output identities for one native build, not correctness evidence."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuiltFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class BuildOutputs:
    workspace: Path
    source_digest: str
    target: str
    files: tuple[BuiltFile, ...]


def _output_paths(workspace: Path) -> tuple[Path, ...]:
    paths = {*workspace.glob("CppExtension/*.so"),
             *workspace.glob("*/build_out/custom_opp_*.run")}
    if not any(path.parent == workspace / "CppExtension" for path in paths):
        raise ValueError("native build produced no job-local extension")
    installed = workspace / "opp/vendors/customize"
    if not installed.is_dir():
        raise ValueError("native build produced no job-local operator installation")
    for path in installed.rglob("*"):
        if not path.resolve(strict=True).is_relative_to(workspace.resolve()):
            raise ValueError("installed native output escapes the native job")
        if path.is_symlink() and path.is_dir():
            raise ValueError("directory links in installed native outputs are not qualified")
        if path.is_file():
            paths.add(path)
    for path in paths:
        if not path.resolve(strict=True).is_relative_to(workspace.resolve()):
            raise ValueError("build output escapes the native job")
    return tuple(sorted(paths))


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def capture_outputs(workspace: Path, source_digest: str, target: str) -> BuildOutputs:
    files = tuple(BuiltFile(path, _file_digest(path)) for path in _output_paths(workspace))
    return BuildOutputs(workspace, source_digest, target, files)


def verify_outputs(outputs: BuildOutputs, workspace: Path) -> None:
    if outputs.workspace != workspace or not outputs.files:
        raise ValueError("build outputs belong to another native job")
    if {item.path for item in outputs.files} != set(_output_paths(workspace)):
        raise ValueError("installed native output inventory changed")
    for item in outputs.files:
        if _file_digest(item.path) != item.sha256:
            raise ValueError(f"native build output changed: {item.path.name}")
