"""Observed, bounded parser identity; no source, clock or caller claims are executed."""
from __future__ import annotations

import codecs
import csv
import dataclasses
import hashlib
import importlib
import json
import marshal
import operator
import re
import struct
import sys
import sysconfig
from pathlib import Path, PurePosixPath
from types import BuiltinFunctionType, CodeType, FunctionType, ModuleType, WrapperDescriptorType
from typing import Any

from kernelcat_executor.contracts.models import Json, canonical, digest
from pydantic_core import PydanticUndefined

from kernelcat_ingestion.plans import Snapshot

RECIPE = "native-lossless/v2"
# Explicit closure, independent of incidental sys.modules/import order.
_MODULES = (
    "struct", "_struct", "codecs", "encodings.utf_8", "encodings.utf_8_sig",
    "ast", "csv", "io", "json", "json.decoder", "json.encoder", "json.scanner",
    "pathlib", "re", "re._compiler", "re._parser", "re._constants", "_csv", "_json",
    "yaml", "yaml.composer", "yaml.constructor", "yaml.error", "yaml.events",
    "yaml.loader", "yaml.nodes", "yaml.parser", "yaml.reader", "yaml.resolver",
    "yaml.scanner", "yaml.tokens", "kernelcat_ingestion.parsers.native",
    "kernelcat_ingestion.parsers.identity", "kernelcat_ingestion.plans",
    "kernelcat_executor.contracts.base", "kernelcat_executor.contracts.models", "pydantic.main",
)


def _file_digest(path: Path) -> str:
    if path.stat().st_size > 134217728:
        raise ValueError(f"parser identity input exceeds 128 MiB: {path.name}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _code_digest(code: CodeType) -> str:
    # Admit all copying/serialization work before normalizing code locations.
    _CodeAdmission().visit(code)
    return hashlib.sha256(marshal.dumps(_normalized_code(code))).hexdigest()


def _normalized_code(code: CodeType) -> CodeType:
    return code.replace(co_filename="", co_firstlineno=1, co_consts=tuple(
        _normalized_code(value) if isinstance(value, CodeType) else value
        for value in code.co_consts))


class ObservationError(ValueError):
    """The effective parser state cannot be represented within the supported bounds."""


class _CodeAdmission:
    """Bound marshal inputs before copying; reject shared or container-nested code."""

    def __init__(self) -> None:
        self.edges = 0
        self.remaining_bytes = 262144
        self.codes: set[int] = set()
        self.values: dict[int, Any] = {}

    def charge(self, size: int) -> None:
        self.remaining_bytes -= size
        if self.remaining_bytes < 0:
            raise ObservationError("parser code observation exceeds 256 KiB budget")

    def visit(self, value: Any, depth: int = 0, *, allow_code: bool = True) -> None:
        self.edges += 1
        self.charge(128)
        if self.edges > 4096 or depth > 32:
            raise ObservationError("parser code observation exceeds 4096 edges or depth 32")
        if isinstance(value, CodeType):
            if not allow_code or id(value) in self.codes:
                raise ObservationError("parser observation unsupported shared code or nested code")
            self.codes.add(id(value))
            self.code(value, depth + 1)
            return
        if id(value) in self.values:
            return
        self.values[id(value)] = value
        if type(value) in {tuple, frozenset}:
            for item in value:
                self.visit(item, depth + 1, allow_code=False)
        elif type(value) in {str, bytes}:
            self.charge(6 * len(value))
        elif type(value) is int:
            self.charge(value.bit_length() + 1)
        elif value is not None and value is not Ellipsis and type(value) not in {bool, float, complex}:
            name = type(value).__module__ + "." + type(value).__qualname__
            raise ObservationError("parser code observation unsupported constant: " + name)

    def code(self, code: CodeType, depth: int) -> None:
        # Numeric header fields are fixed-width. Filename/first line are normalized
        # away; every variable-size field retained by marshal is admitted here.
        for value in (code.co_code, code.co_linetable, code.co_exceptiontable,
                      code.co_name, code.co_qualname, code.co_names, code.co_varnames,
                      code.co_freevars, code.co_cellvars):
            self.visit(value, depth, allow_code=False)
        for value in code.co_consts:
            self.visit(value, depth)


class _Observation:
    """One local graph; references avoid expanding shared defaults/closures as trees."""

    def __init__(self) -> None:
        self.nodes: list[Any] = []
        self.observed: list[Any] = []
        self.references: dict[int, int] = {}
        self.edges = 0
        self.remaining_bytes = 262144

    def charge(self, size: int) -> None:
        self.remaining_bytes -= size
        if self.remaining_bytes < 0:
            raise ObservationError("parser observation exceeds 256 KiB component budget")

    def encode(self, value: Any, depth: int = 0) -> Any:
        self.edges += 1
        self.charge(128)  # Covers tags, references and node separators before allocation.
        if self.edges > 4096 or depth > 32:
            raise ObservationError("parser observation exceeds 4096 edges or depth 32")
        if value is None or type(value) in {str, bytes, int, bool, float}:
            return self.scalar(value)
        previous = self.references.get(id(value))
        if previous is not None:
            return {"ref": previous}
        index = len(self.nodes)
        self.references[id(value)] = index
        self.nodes.append(None)
        self.observed.append(value)
        self.nodes[index] = self.node(value, depth + 1)
        return {"ref": index}

    def scalar(self, value: Any) -> Any:
        if type(value) in {str, bytes}:
            self.charge(6 * len(value))
            payload = value.hex() if type(value) is bytes else value
        elif type(value) is int:
            self.charge(value.bit_length() + 1)
            payload = hex(value)
        elif type(value) is float:
            payload = struct.pack("!d", value).hex()
        else:
            payload = value
        return {"type": type(value).__name__, "value": payload}

    def node(self, value: Any, depth: int) -> Json:
        if type(value) in {list, tuple}:
            return {"type": type(value).__name__,
                    "items": [self.encode(item, depth) for item in value]}
        if type(value) is dict:
            # Captured mappings may be iterated by a parser: insertion order matters.
            return {"type": "dict", "items": [[self.encode(key, depth), self.encode(item, depth)]
                                              for key, item in value.items()]}
        if isinstance(value, FunctionType):
            return {"type": "function", "code": _code_digest(value.__code__),
                    "defaults": self.encode(value.__defaults__, depth),
                    "keywords": self.encode(value.__kwdefaults__, depth),
                    "closure": [self.encode(cell.cell_contents, depth)
                                for cell in value.__closure__ or ()]}
        if isinstance(value, re.Pattern):
            return {"type": "regex", "pattern": self.encode(value.pattern, depth),
                    "flags": value.flags}
        if type(value) is operator.itemgetter:
            return {"type": "itemgetter", "arguments": self.encode(value.__reduce__()[1], depth)}
        if type(value) in {set, frozenset} and not value:
            return {"type": type(value).__name__, "items": []}
        return self.native(value, depth)

    def native(self, value: Any, depth: int) -> Json:
        if value is dataclasses.MISSING:
            return {"sentinel": "dataclasses.MISSING"}
        if value is PydanticUndefined:
            return {"sentinel": "pydantic_core.PydanticUndefined"}
        if isinstance(value, BuiltinFunctionType):
            owner = value.__self__
            bound = owner.__name__ if isinstance(owner, ModuleType) else self.encode(owner, depth)
            return {"native_function": str(value.__module__) + "." + value.__qualname__,
                    "owner": bound}
        if isinstance(value, WrapperDescriptorType) and value.__objclass__.__module__ == "builtins":
            return {"native_slot": value.__objclass__.__qualname__ + "." + value.__name__}
        if isinstance(value, type) and (
            value.__module__ in {*_MODULES, "builtins", "enum", "dataclasses", "yaml.dumper"}
            or value.__module__.startswith("pydantic.")
        ):
            return {"type_reference": value.__module__ + "." + value.__qualname__}
        name = type(value).__module__ + "." + type(value).__qualname__
        raise ObservationError("parser observation unsupported value: " + name)


def _value(value: Any) -> Json:
    observation = _Observation()
    root = observation.encode(value)
    return {"root": root, "nodes": observation.nodes}


def _module_identity(module: ModuleType) -> Json:
    functions: Json = {}
    for name, value in sorted(vars(module).items()):
        if isinstance(value, FunctionType):
            functions[name] = value
        elif isinstance(value, type) and value.__module__ == module.__name__:
            for key, member in sorted(vars(value).items()):
                if isinstance(member, (classmethod, staticmethod)):
                    member = member.__func__
                if isinstance(member, FunctionType):
                    functions[name + "." + key] = member
    origin = module.__spec__.origin if module.__spec__ else None
    if origin in {"built-in", "frozen"}:
        content = origin
    elif module.__file__:
        content = _file_digest(Path(module.__file__))
    else:
        raise ValueError(f"parser module origin unavailable: {module.__name__}")
    return {"content": content, "loaded_code": digest(_value(functions))}


def _package_digest(name: str) -> str:
    """Pydantic's serialization/validation uses Python and compiled extensions."""
    module = importlib.import_module(name)
    if module.__file__ is None:
        raise ValueError(f"parser dependency origin unavailable: {name}")
    root = Path(module.__file__).parent
    files = sorted(path for path in root.rglob("*") if path.suffix in {".py", ".so", ".pyd"})
    if not files or len(files) > 1024:
        raise ValueError(f"parser dependency file count outside 1..1024: {name}")
    return digest({path.relative_to(root).as_posix(): _file_digest(path) for path in files})


def _runtime_identity() -> Json:
    library = sysconfig.get_config_var("LDLIBRARY")
    runtime = {"executable": _file_digest(Path(sys.executable))}
    if sysconfig.get_config_var("PYTHONFRAMEWORK"):
        runtime["library"] = _file_digest(Path(sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX")) / library)
    elif sysconfig.get_config_var("Py_ENABLE_SHARED"):
        runtime["library"] = _file_digest(Path(sysconfig.get_config_var("LIBDIR")) / library)
    return runtime


def _yaml_loader_digest(native: ModuleType) -> str:
    return digest(_value({
        "constructors": native.UniqueYAML.yaml_constructors,
        "escape_replacements": native.UniqueYAML.ESCAPE_REPLACEMENTS,
        "escape_codes": native.UniqueYAML.ESCAPE_CODES,
        "default_tags": native.UniqueYAML.DEFAULT_TAGS,
        "default_scalar_tag": native.UniqueYAML.DEFAULT_SCALAR_TAG,
        "default_sequence_tag": native.UniqueYAML.DEFAULT_SEQUENCE_TAG,
        "default_mapping_tag": native.UniqueYAML.DEFAULT_MAPPING_TAG,
        "non_printable": native.UniqueYAML.NON_PRINTABLE,
        "bool_values": native.UniqueYAML.bool_values,
        "multi_constructors": native.UniqueYAML.yaml_multi_constructors,
        "resolvers": native.UniqueYAML.yaml_implicit_resolvers,
        "path_resolvers": native.UniqueYAML.yaml_path_resolvers,
    }))


def observe_environment() -> str:
    """Capture once at a parse/runtime boundary; returned canonical JSON is immutable."""
    modules = {name: _module_identity(importlib.import_module(name)) for name in _MODULES}
    native = importlib.import_module("kernelcat_ingestion.parsers.native")
    yaml = importlib.import_module("yaml")
    descriptor = {
        "recipe": RECIPE,
        "python": {"implementation": sys.implementation.name, "version": list(sys.version_info),
                   "grammar_feature_version": list(sys.version_info[:2]), "runtime": _runtime_identity(),
                   "recursion_limit": sys.getrecursionlimit(),
                   "int_max_str_digits": sys.get_int_max_str_digits()},
        "implementation_digest": digest(modules),
        "dependencies": {"PyYAML": {"version": yaml.__version__,
                                      "implementation_digest": digest({key: value for key, value
                                          in modules.items() if key.startswith("yaml")})},
                         **{name: _package_digest(name) for name in ("pydantic", "pydantic_core")}},
        "yaml_loader_digest": _yaml_loader_digest(native),
        "decoder_digest": digest(_value({"decode": codecs.lookup("utf-8-sig").decode,
                                         "strict_errors": codecs.lookup_error("strict"),
                                         "bom_utf8": codecs.BOM_UTF8})),
        "csv_field_size_limit": csv.field_size_limit(),
        "format_rules": {"code": sorted(native.CODE), "text": sorted(native.TEXT)},
    }
    result = canonical(descriptor)
    if len(result.encode()) > 16384:
        raise ValueError("parser environment descriptor exceeds 16 KiB")
    return result


def interpretation(environment: str, snapshot: Snapshot, chunk_lines: int) -> Json:
    return {"recipe": RECIPE, "environment": json.loads(environment),
            "options": {"chunk_lines": chunk_lines, "encoding": "utf-8-sig",
                        "python_mode": "exec", "python_type_comments": False,
                        "python_feature_version": None},
            "format": {"name": snapshot.name, "suffix": PurePosixPath(snapshot.name).suffix.lower(),
                       "role": snapshot.role}, "artifact_digest": snapshot.artifact_digest}
