"""Finite source-deployment observation; no report or version is an execution permit.

Deployments must be immutable during observation/import/dispatch. Hostile import
hooks and concurrent installation are outside this source-loader qualification.
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import cast
from urllib.parse import unquote, urlsplit

from kernelcat_knowledge.contracts.schema import (
    validate_definition,
    wire_bytes,
    wire_digest,
)
from kernelcat_knowledge.generated.python_modules import (
    SCHEMA,
    ExpectedPythonModules,
    KnownPythonModuleOrigin,
    ModuleName,
    PythonModuleObservation,
    PythonModuleOrigin,
    PythonModuleReport,
)

MAX_ENTRIES = 20_000
MAX_FILES = 10_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PATH_BYTES = 1024
MAX_BINDING_BYTES = 256 * 1024
MAX_REPORT_BYTES = 64 * 1024
PRODUCT_MODULES: tuple[ModuleName, ...] = (
    "kernelcat_executor", "kernelcat_ingestion", "kernelcat_knowledge", "kernelcat_worker",
)


class ModuleAdmissionError(ValueError):
    """Observation failed or did not match the independently supplied binding."""


def _canonical_path(path: Path) -> Path:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ModuleAdmissionError(f"noncanonical or symlinked path: {path}")
    return path


def _root_uri(uri: str) -> Path:
    parts = urlsplit(uri)
    path = Path(unquote(parts.path, encoding="utf-8", errors="strict"))
    if parts.scheme != "file" or parts.netloc or parts.query or parts.fragment:
        raise ModuleAdmissionError(f"not a local file URI: {uri}")
    if not path.is_absolute() or path.as_uri() != uri:
        raise ModuleAdmissionError(f"noncanonical root URI: {uri}")
    _canonical_path(path)
    if not path.is_dir():
        raise ModuleAdmissionError(f"package root is not a directory: {path}")
    return path


def normalize_expected(value: object, required: tuple[ModuleName, ...]) -> ExpectedPythonModules:
    """Validate permission/configuration independently of selected import roots."""
    validate_definition(value, SCHEMA, "ExpectedPythonModules")
    normalized = cast(ExpectedPythonModules, json.loads(wire_bytes(value)))
    names = [entry["module_name"] for entry in normalized["modules"]]
    if len(names) != len(set(names)) or set(names) != set(required):
        raise ModuleAdmissionError("binding must contain the exact required module set once")
    physical_roots: set[tuple[int, int]] = set()
    for entry in normalized["modules"]:
        name = entry["module_name"]
        if entry["distribution_name"] != name.replace("_", "-"):
            raise ModuleAdmissionError(f"distribution/name mismatch: {name}")
        for uri in entry["allowed_package_roots"]:
            metadata = _root_uri(uri).stat()
            identity = (metadata.st_dev, metadata.st_ino)
            if identity in physical_roots:
                raise ModuleAdmissionError(f"duplicate physical root: {uri}")
            physical_roots.add(identity)
        entry["allowed_package_roots"].sort(key=lambda uri: uri.encode("utf-8"))
    normalized["modules"].sort(key=lambda entry: entry["module_name"])
    return normalized


def _source_record(path: Path, root: Path) -> tuple[dict[str, str], int]:
    relative = path.relative_to(root).as_posix()
    if (len(relative.encode("utf-8")) > MAX_PATH_BYTES or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ModuleAdmissionError(f"unsupported source path: {relative}")
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & 0o444:
        raise ModuleAdmissionError(f"not a readable regular source: {path}")
    if metadata.st_size > MAX_FILE_BYTES:
        raise ModuleAdmissionError(f"source exceeds per-file byte bound: {path}")
    with path.open("rb") as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ModuleAdmissionError(f"source exceeds per-file byte bound: {path}")
    return {"path": relative, "sha256": hashlib.sha256(data).hexdigest()}, len(data)


def source_digest(root: Path) -> str:
    """O(B + E + F log F) time; bounded traversal and O(F + E) storage."""
    _canonical_path(root)
    files: list[dict[str, str]] = []
    pending = [root]
    entries = total_bytes = 0
    while pending:
        with os.scandir(pending.pop()) as children:
            for child in children:
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ModuleAdmissionError("source tree exceeds directory-entry bound")
                if child.is_symlink():
                    raise ModuleAdmissionError(f"symlinked source tree entry: {child.path}")
                path = Path(child.path)
                # Case aliases can resolve to the same cache, which -B still reads.
                if child.name.casefold() == "__pycache__" or path.suffix.casefold() in {".pyc", ".pyo"}:
                    raise ModuleAdmissionError(f"product bytecode/cache is unsupported: {path}")
                if child.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif child.name.endswith(".py"):
                    record, size = _source_record(path, root)
                    files.append(record)
                    total_bytes += size
                    if len(files) > MAX_FILES or total_bytes > MAX_TOTAL_BYTES:
                        raise ModuleAdmissionError("source tree exceeds file-count or total-byte bound")
    files.sort(key=lambda record: record["path"].encode("utf-8"))
    if not any(record["path"] == "__init__.py" for record in files):
        raise ModuleAdmissionError(f"missing package source: {root / '__init__.py'}")
    return wire_digest({"files": files, "hash_rule": "python-source-tree/v1"})


def _spec_path(spec: importlib.machinery.ModuleSpec | None, name: str) -> Path:
    if spec is None or spec.name != name:
        raise ModuleAdmissionError(f"missing/inconsistent spec: {name}")
    if type(spec.loader) is not importlib.machinery.SourceFileLoader:
        raise ModuleAdmissionError(f"unsupported loader: {name}")
    if spec.origin is None or spec.loader.name != name or spec.loader.path != spec.origin:
        raise ModuleAdmissionError(f"inconsistent loader/origin: {name}")
    path = _canonical_path(Path(spec.origin))
    if str(path) != spec.origin:
        raise ModuleAdmissionError(f"noncanonical source origin: {name}")
    if path.suffix != ".py" or not path.is_file():
        raise ModuleAdmissionError(f"unsupported source origin: {name}")
    locations = spec.submodule_search_locations
    if locations is not None and list(locations) != [str(path.parent)]:
        raise ModuleAdmissionError(f"inconsistent package search locations: {name}")
    if locations is not None and path.name != "__init__.py":
        raise ModuleAdmissionError(f"unsupported package source: {name}")
    return path


def _observe(name: ModuleName) -> PythonModuleOrigin:
    try:
        spec = importlib.util.find_spec(name)
        path = _spec_path(spec, name)
        if spec is None or spec.submodule_search_locations is None:
            raise ModuleAdmissionError(f"required module is not a package: {name}")
        return {
            "state": "known", "module_origin": path.as_uri(),
            "package_root": path.parent.as_uri(), "hash_rule": "python-source-tree/v1",
            "content_digest": source_digest(path.parent),
        }
    except (ModuleAdmissionError, OSError, ValueError, ImportError) as error:
        return {"state": "unknown", "reason": f"{name}: {error}"[:2048]}


def _loaded_path(module: ModuleType, name: str, root: Path) -> None:
    spec = module.__spec__
    path = _spec_path(spec, name)
    assert spec is not None  # _spec_path rejects absent specs at the boundary.
    relative_name = name.split(".")[1:]
    expected_path = root.joinpath(*relative_name)
    if spec.submodule_search_locations is not None:
        expected_path /= "__init__.py"
    else:
        expected_path = expected_path.with_suffix(".py")
    if path != expected_path or module.__file__ != str(path):
        raise ModuleAdmissionError(f"inconsistent loaded file: {name}")
    if module.__loader__ is not spec.loader:
        raise ModuleAdmissionError(f"inconsistent loaded loader: {name}")
    if (spec.submodule_search_locations is not None
            and list(module.__path__) != [str(path.parent)]):
        raise ModuleAdmissionError(f"inconsistent loaded namespace: {name}")


@dataclass(frozen=True, init=False)
class AdmittedModules:
    """Process-local result created only by observation; detached report is evidence."""

    _report_bytes: bytes

    def __init__(self) -> None:
        raise TypeError("use admit_modules with an independent expected binding")

    @property
    def report(self) -> PythonModuleReport:
        return cast(PythonModuleReport, json.loads(self._report_bytes))

    def verify_loaded(self) -> None:
        """Check product namespaces again after the fixed entry module is imported."""
        for observation in self.report["observations"]:
            name = observation["module_name"]
            if name not in sys.modules:
                raise ModuleAdmissionError(f"required package is not loaded: {name}")
            origin = cast(KnownPythonModuleOrigin, observation["origin"])
            root = _root_uri(origin["package_root"])
            for loaded_name, module in tuple(sys.modules.items()):
                if loaded_name == name or loaded_name.startswith(name + "."):
                    if not isinstance(module, ModuleType):
                        raise ModuleAdmissionError(f"invalid loaded module: {loaded_name}")
                    try:
                        _loaded_path(module, loaded_name, root)
                    except AttributeError as error:
                        raise ModuleAdmissionError(f"missing loaded location: {loaded_name}") from error

    def export(self, destination: Path) -> None:
        """Explicit create-only diagnostic artifact; no directory creation or stdout."""
        # Resolve parent aliases before checking and opening the same destination.
        # Keep the final component for O_EXCL, including dangling-symlink collisions.
        target = destination.parent.resolve(strict=True) / destination.name
        protected_roots: set[tuple[int, int]] = set()
        for observation in self.report["observations"]:
            origin = cast(KnownPythonModuleOrigin, observation["origin"])
            metadata = _root_uri(origin["package_root"]).stat()
            protected_roots.add((metadata.st_dev, metadata.st_ino))
        # Path.resolve preserves case aliases on case-insensitive filesystems.
        # Compare actual directory identities rather than spelling/prefixes.
        for parent in (target.parent, *target.parent.parents):
            metadata = parent.stat()
            if (metadata.st_dev, metadata.st_ino) in protected_roots:
                raise ModuleAdmissionError(f"startup report is inside an admitted package: {target}")
        with target.open("xb") as output:
            output.write(self._report_bytes)
            output.flush()
            os.fsync(output.fileno())


def _observe_expected(value: object, required: tuple[ModuleName, ...]) -> PythonModuleReport:
    if sys.pycache_prefix is not None:
        raise ModuleAdmissionError("external Python bytecode cache prefix is unsupported")
    expected = normalize_expected(value, required)
    observations: list[PythonModuleObservation] = []
    for entry in expected["modules"]:
        name = entry["module_name"]
        origin = _observe(name)
        observations.append({"module_name": name, "origin": origin})
        if origin["state"] == "unknown":
            raise ModuleAdmissionError(f"required origin unknown: {origin['reason']}")
        if origin["package_root"] not in entry["allowed_package_roots"]:
            raise ModuleAdmissionError(f"unlisted package root: {name}: {origin['package_root']}")
        if origin["content_digest"] != entry["content_digest"]:
            raise ModuleAdmissionError(f"source content mismatch: {name}")
    report: PythonModuleReport = {
        "schema_version": "kernelcat.python-module-report/v1",
        "expected_binding_digest": wire_digest(expected), "observations": observations,
    }
    validate_definition(report, SCHEMA, "PythonModuleReport")
    return report


def preflight_modules(value: object, required: tuple[ModuleName, ...]) -> None:
    """Check selected source before import; this phase grants no admission."""
    _observe_expected(value, required)


def admit_modules(value: object, required: tuple[ModuleName, ...]) -> AdmittedModules:
    """Re-observe bytes and require loaded packages; never import a capability.

    The trusted composition owner preflights, imports its fixed required set,
    then calls this final admission check before entry dispatch.
    """
    report = _observe_expected(value, required)
    encoded = wire_bytes(report)
    if len(encoded) > MAX_REPORT_BYTES:
        raise ModuleAdmissionError("startup report exceeds byte bound")
    admitted = object.__new__(AdmittedModules)
    object.__setattr__(admitted, "_report_bytes", encoded)
    admitted.verify_loaded()
    return admitted


def read_expected(path: Path) -> object:
    """Read bounded operator input; duplicate JSON keys fail rather than overwrite."""
    if not stat.S_ISREG(path.stat().st_mode):
        raise ModuleAdmissionError(f"binding input is not a regular file: {path}")
    with path.open("rb") as source:
        data = source.read(MAX_BINDING_BYTES + 1)
    if len(data) > MAX_BINDING_BYTES:
        raise ModuleAdmissionError("expected binding exceeds byte bound")
    return json.loads(data, object_pairs_hook=_unique_object)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ModuleAdmissionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
