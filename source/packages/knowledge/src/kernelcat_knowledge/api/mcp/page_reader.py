"""Compatibility re-export for the shared filesystem page adapter."""
from __future__ import annotations

from kernelcat_knowledge.storage.pages import PageReader, parse_frontmatter

__all__ = ["PageReader", "parse_frontmatter"]
