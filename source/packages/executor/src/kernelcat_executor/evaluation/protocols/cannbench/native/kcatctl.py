#!/usr/bin/env python3
"""Stable launcher for the KernelCat 910C control plane."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kernelcat_executor.evaluation.protocols.cannbench.native.control_plane.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
