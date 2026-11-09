"""Resolve immutable resource identities independently of task names or directory layout."""
from kernelcat_knowledge.resources.catalog import (
    ResourceCatalog,
    ResourceError,
    ResourceReference,
)

__all__ = ["ResourceCatalog", "ResourceError", "ResourceReference"]
