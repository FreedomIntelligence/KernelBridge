"""Identity of loaded query policy, never a hash of subsequently edited source files."""

from __future__ import annotations

import importlib
import re
import sys
from dataclasses import FrozenInstanceError
from datetime import timezone
from types import CodeType, FunctionType
from typing import Any, TypeVar

from kernelcat_knowledge.contracts import ContractError, sha256_digest
from kernelcat_knowledge.generated.knowledge_query import SCHEMA

_POLICY_MODULES = (
    "contracts.validation",
    "contracts.history",
    "contracts.schema",
    "contracts.stage2",
    "contracts.query",
    "contracts.runtime",
    "query.catalog",
    "query.projection",
    "query.results",
    "query.identity",
    "cards.active",
    "cards.authority",
    "storage.sources",
    "storage.catalog_io",
    "storage.artifacts",
    "cards.eligibility",
    "cards.legacy",
    "storage.selection",
    "retained.control",
    "retained.view",
    "storage.filesystem",
    "storage.index",
    "evidence.source_content",
    "query.filters",
    "contracts.canonical",
)


class _PolicyEncoder:
    """Bounded canonical traversal of loaded code and its captured executable state."""

    def __init__(self) -> None:
        self.remaining = 500_000
        self.active: set[int] = set()

    def encode(self, value: Any) -> Any:
        self.remaining -= 1
        if self.remaining < 0 or len(self.active) >= 64:
            raise ContractError("loaded policy identity exceeds traversal bound")
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if id(value) in self.active:
            raise ContractError("loaded policy identity contains a cycle")
        self.active.add(id(value))
        try:
            return self._composite(value)
        finally:
            self.active.remove(id(value))

    def _composite(self, value: Any) -> Any:
        if isinstance(value, CodeType):
            return self._code(value)
        if isinstance(value, FunctionType):
            return self._function(value)
        if isinstance(value, re.Pattern):
            return {"pattern": value.pattern, "flags": value.flags}
        if isinstance(value, (set, frozenset)):
            return sorted((self.encode(item) for item in value), key=str)
        if isinstance(value, (tuple, list)):
            return [self.encode(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self.encode(item) for key, item in value.items()}
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        if isinstance(value, type):
            return self._type_reference(value)
        if isinstance(value, timezone):
            return {
                "timezone": value.tzname(None),
                "seconds": value.utcoffset(None).total_seconds(),
            }
        if isinstance(value, TypeVar):
            return {
                "typevar": value.__name__,
                "bound": self.encode(value.__bound__),
                "constraints": self.encode(value.__constraints__),
                "covariant": value.__covariant__,
                "contravariant": value.__contravariant__,
            }
        if value is Ellipsis:
            return {"constant": "Ellipsis"}
        raise ContractError(
            f"unsupported loaded policy identity state: {type(value).__name__}"
        )

    @staticmethod
    def _type_reference(value: type) -> dict[str, str]:
        # Generated dataclass methods and super() capture links to class bodies
        # encoded separately below; arbitrary unencoded classes are unsupported.
        module = sys.modules.get(value.__module__)
        owned = value.__module__.removeprefix("kernelcat_knowledge.") in _POLICY_MODULES
        if value in (object, FrozenInstanceError) or (
            owned and module is not None and vars(module).get(value.__name__) is value
        ):
            return {"type_reference": value.__module__ + "." + value.__qualname__}
        raise ContractError("unsupported captured policy class")

    def _function(self, function: FunctionType) -> dict[str, Any]:
        try:
            captured = [cell.cell_contents for cell in (function.__closure__ or ())]
        except ValueError as error:
            raise ContractError(
                "loaded policy identity contains an empty closure"
            ) from error
        return {
            "code": self.encode(function.__code__),
            "defaults": self.encode(function.__defaults__),
            "kwdefaults": self.encode(function.__kwdefaults__),
            "closure": self.encode(captured),
        }

    def _code(self, code: CodeType) -> dict[str, Any]:
        return {
            "bytecode": code.co_code.hex(),
            "constants": self.encode(code.co_consts),
            "names": code.co_names,
            "variables": code.co_varnames,
            "freevars": code.co_freevars,
            "cellvars": code.co_cellvars,
            "args": code.co_argcount,
            "posonly": code.co_posonlyargcount,
            "kwonly": code.co_kwonlyargcount,
            "flags": code.co_flags,
            "exceptions": code.co_exceptiontable.hex(),
        }


def policy_digest() -> str:
    """O(loaded policy size) once per composition, independent of checkout paths."""
    modules = {}
    encoder = _PolicyEncoder()
    for name in _POLICY_MODULES:
        module = importlib.import_module("kernelcat_knowledge." + name)
        members = {}
        for key, value in vars(module).items():
            if isinstance(value, FunctionType) and value.__module__ == module.__name__:
                members[key] = encoder.encode(value)
            elif isinstance(value, type) and value.__module__ == module.__name__:
                members[key] = {
                    method: encoder.encode(
                        function.__func__
                        if isinstance(function, (staticmethod, classmethod))
                        else function
                    )
                    for method, function in vars(value).items()
                    if isinstance(function, (FunctionType, staticmethod, classmethod))
                }
            elif key.isupper() and not key.startswith("__"):
                members[key] = encoder.encode(value)
        modules[name] = members
    return sha256_digest(
        {"schema": SCHEMA, "policy": modules, "python": list(sys.version_info[:2])}
    )


def query_binding(
    *,
    schema_digest: str,
    authority: str | None = None,
    config_digest: str | None = None,
    view_digest: str | None = None,
) -> dict[str, Any]:
    status = (
        "injected_repository_unknown"
        if authority is None
        else "legacy_capture_unknown"
        if config_digest is None
        else "bound"
    )
    return {
        "status": status,
        "corpus_authority_id": authority,
        "config_digest": config_digest,
        "schema_digest": schema_digest,
        "retained_view_digest": view_digest,
    }
