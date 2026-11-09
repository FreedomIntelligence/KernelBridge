"""Admit explicit implementation bindings at the concrete Worker boundary."""
from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from importlib.machinery import PathFinder
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

from kernelcat_worker.configuration import Config
from kernelcat_worker.generated.python_startup import (
    PRODUCT_MODULES,
    AdmittedModules,
    ExpectedPythonModules,
    KnownPythonModuleOrigin,
    ModuleAdmissionError,
    PythonModuleReport,
    admit_modules,
    normalize_expected,
    preflight_modules,
    read_expected,
    wire_bytes,
)


def expected_input(path: Path | None, injected: object | None) -> ExpectedPythonModules:
    """Resolve operator inputs without replacing one authority with another."""
    if path is None and injected is None:
        raise ModuleAdmissionError("explicit expected Python modules are required")
    if path is not None and not path.is_absolute():
        raise ModuleAdmissionError("expected Python modules path must be absolute")
    value = read_expected(path) if path is not None else injected
    expected = normalize_expected(value, PRODUCT_MODULES)
    if injected is not None and expected != normalize_expected(injected, PRODUCT_MODULES):
        raise ModuleAdmissionError("startup input differs from the trusted launcher binding")
    return expected


@dataclass(frozen=True, init=False)
class PythonStartup:
    """An in-process admission, with detached evidence for the existing owner."""

    _expected_bytes: bytes
    _admitted: AdmittedModules

    def __init__(self) -> None:
        raise TypeError("use PythonStartup.admit with an independent expected binding")

    @classmethod
    def admit(cls, expected_modules: object) -> PythonStartup:
        expected = normalize_expected(expected_modules, PRODUCT_MODULES)
        preflight_modules(expected, PRODUCT_MODULES)
        for name in PRODUCT_MODULES:
            importlib.import_module(name)
        admitted = admit_modules(expected, PRODUCT_MODULES)
        result = object.__new__(cls)
        object.__setattr__(result, "_expected_bytes", wire_bytes(expected))
        object.__setattr__(result, "_admitted", admitted)
        return result

    @property
    def expected(self) -> ExpectedPythonModules:
        return cast(ExpectedPythonModules, json.loads(self._expected_bytes))

    @property
    def report(self) -> PythonModuleReport:
        return self._admitted.report

    @property
    def knowledge_expected(self) -> ExpectedPythonModules:
        expected = self.expected
        expected["modules"] = [entry for entry in expected["modules"]
                               if entry["module_name"] == "kernelcat_knowledge"]
        return expected

    @property
    def import_roots(self) -> tuple[Path, ...]:
        """Child import paths come from actual admission, not the first allowed copy."""
        self._admitted.verify_loaded()
        # Admission accepts only known ordinary-source origins.
        origins = {row["module_name"]: cast(KnownPythonModuleOrigin, row["origin"])
                   for row in self.report["observations"]}
        parents = {Path(unquote(urlsplit(origin["package_root"]).path)).parent
                   for origin in origins.values()}
        roots = tuple(dict.fromkeys(Path(path) for path in sys.path if Path(path) in parents))
        if set(roots) != parents:
            raise ModuleAdmissionError("admitted package parents are unavailable to the child")
        # A fresh child has no sys.modules cache. Preserve precedence and check the
        # uncached selection before any caller constructs its external client.
        search_path = list(map(str, roots))
        for name, origin in origins.items():
            spec = PathFinder.find_spec(name, search_path)
            if spec is None or spec.origin != unquote(urlsplit(origin["module_origin"]).path):
                raise ModuleAdmissionError(f"child would select a different origin: {name}")
        return roots

    def bind_config(self, config: Config) -> Config:
        expected = self.expected
        if normalize_expected(config.python_modules, PRODUCT_MODULES) != expected:
            raise ModuleAdmissionError("configuration differs from the admitted Python binding")
        self._admitted.verify_loaded()
        return config.model_copy(update={"python_modules": expected})

    def export(self, destination: Path) -> None:
        self._admitted.export(destination)


def admit_config(config: Config, *, expected_modules: object | None = None) -> PythonStartup:
    expected = normalize_expected(config.python_modules, PRODUCT_MODULES)
    if expected_modules is not None and expected != normalize_expected(expected_modules, PRODUCT_MODULES):
        raise ModuleAdmissionError("configuration differs from the trusted launcher binding")
    return PythonStartup.admit(expected)
