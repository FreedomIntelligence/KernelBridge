"""Resolve current Git-tree paths after a component is nested in a workspace.

Historical source_commit:path references must remain unchanged. Only current HEAD
paths expressed relative to the runtime component need its Git-root prefix.
"""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath


def head_tree_spec(component_root: str | Path, relative_path: str) -> str:
    """Return an unambiguous HEAD:<repository-relative-path> for a component path."""
    path = PurePosixPath(relative_path)
    if not relative_path or path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("backup path must be a nonempty relative component path")
    result = subprocess.run(
        ("git", "-C", str(Path(component_root).resolve()), "rev-parse", "--show-prefix"),
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("cannot resolve component prefix in its Git repository")
    prefix = result.stdout.rstrip("\n")
    return "HEAD:" + prefix + path.as_posix()
