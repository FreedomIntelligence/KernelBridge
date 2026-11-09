"""Locate an explicitly installed Pi adapter without launching it."""
from __future__ import annotations

import os
from pathlib import Path


def default_pi_root() -> str:
    """Resolve a configured SDK root or the fixed source-layout location."""
    configured = os.environ.get("KERNELCAT_PI_WORKER_ROOT")
    if configured is not None:
        return configured
    root = Path(__file__).resolve().parents[5] / "apps/agent"
    return str(root) if (root / "package.json").is_file() else ""
