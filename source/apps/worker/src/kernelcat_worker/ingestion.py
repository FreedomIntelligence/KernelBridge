"""Concrete Ingestion bindings over the single Executor and Knowledge owners."""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import kernelcat_executor
import kernelcat_knowledge
from kernelcat_executor.contracts.models import (
    Action,
    Json,
    Outcome,
    Policy,
    Task,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import Tool
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.state.journal import Journal
from kernelcat_ingestion.cli import main as ingestion_main
from kernelcat_ingestion.evidence import WikiEvidence
from kernelcat_ingestion.pipeline import Pipeline, external_state
from kernelcat_ingestion.plans import SourcePlan
from kernelcat_ingestion.ports import BoundEffects
from kernelcat_ingestion.smoke import smoke
from kernelcat_ingestion.snapshots import DataError
from kernelcat_knowledge.corpus import (
    CorpusError,
    EvidenceStore,
    initialize_corpus,
    open_evidence_store,
)

from kernelcat_worker.startup import PythonStartup, expected_input

T = TypeVar("T")


def _storage(operation: str, call: Callable[[], T]) -> T:
    try:
        return call()
    except CorpusError as error:
        raise DataError(f"{operation}: {error}") from error


class IngestionEvidenceStore:
    """Translate known Knowledge storage failures at the injected boundary."""
    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    def capture(self, identity: Mapping[str, Any], content: bytes) -> Json:
        return _storage("capture ingestion source", lambda: self.store.capture(identity, content))

    def source(self, source_id: str) -> Json:
        return _storage("read ingestion source identity", lambda: self.store.source(source_id))

    def read_source(self, source_id: str) -> bytes:
        return _storage("read ingestion source bytes", lambda: self.store.read_source(source_id))

    def register_evidence(self, record: Mapping[str, Any]) -> None:
        _storage("register ingestion evidence", lambda: self.store.register_evidence(record))

    def evidence(self, evidence_id: str) -> Json | None:
        return _storage("read ingestion evidence", lambda: self.store.evidence(evidence_id))


@dataclass(frozen=True)
class JournalEffects:
    journal: Journal
    gateway: Gateway

    def execute(self, action: Action) -> Outcome:
        return self.gateway.execute(action)

    def snapshot(self) -> Json:
        return self.journal.snapshot()

    def task_lock(self, task_id: str) -> AbstractContextManager[None]:
        return self.journal.task_lock(task_id)


def _runtime_fingerprint(ingestion_digest: str) -> str:
    """Bind the injected implementations where their concrete owners are selected."""
    roots = {"executor": Path(kernelcat_executor.__file__).parent,
             "knowledge": Path(kernelcat_knowledge.__file__).parent,
             "worker": Path(__file__).parent}
    files = {domain + "/" + path.relative_to(root).as_posix():
             hashlib.sha256(path.read_bytes()).hexdigest()
             for domain, root in roots.items() for path in sorted(root.rglob("*.py"))}
    return digest({"ingestion": ingestion_digest, "bindings": files})


@dataclass(frozen=True)
class JournalBinding:
    state: Path
    startup: PythonStartup

    def bind(self, task: Task, tools: list[Tool], *, credits: int,
             resource_limits: dict[str, int], runtime_digest: str) -> BoundEffects:
        journal = Journal(external_state(self.state) / "journal.jsonl")
        journal.initialize(credits, Policy(), resource_limits=resource_limits)
        journal.bind_runtime(_runtime_fingerprint(runtime_digest))
        journal.record_python_modules(self.startup.expected, self.startup.report)
        journal.register_task(task.model_dump(mode="json"))
        return JournalEffects(journal, Gateway(journal, tools, task.scope))


class ConfiguredIngestion:
    def __init__(self, *, expected_modules: object, startup_report: Path | None = None) -> None:
        self.startup = PythonStartup.admit(expected_modules)
        self.startup_report = startup_report
        if startup_report is not None:
            if not startup_report.is_absolute():
                raise ValueError("startup report must be an absolute path")
            self.startup.export(startup_report)

    def initialize(self, root: Path) -> None:
        if self.startup_report is None:
            raise ValueError("ingestion initialization requires an explicit startup report")
        resolved = external_state(root)
        _storage("initialize ingestion evidence corpus", lambda: initialize_corpus(resolved))

    def collect(self, state: Path, evidence_root: Path, plan: SourcePlan, *,
                authorize_network: bool) -> Json:
        root = external_state(evidence_root)
        store = _storage("open ingestion evidence corpus", lambda: open_evidence_store(root))
        evidence = WikiEvidence(root, store=IngestionEvidenceStore(store))
        pipeline = Pipeline(state, plan, effects=JournalBinding(state, self.startup), evidence=evidence,
                            authorize_network=authorize_network)
        return pipeline.collect()


def main(argv: list[str] | None = None, *, expected_modules: object | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    startup_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    startup_parser.add_argument("--expected-python-modules", type=Path)
    startup_parser.add_argument("--startup-report", type=Path)
    startup_args, values = startup_parser.parse_known_args(values)
    is_smoke = bool(values) and values[0] == "smoke"
    if not is_smoke and (not values or "--help" in values or "-h" in values):
        return ingestion_main(values)
    smoke_args = _smoke_arguments(values[1:]) if is_smoke else None
    try:
        expected = expected_input(startup_args.expected_python_modules, expected_modules)
        if (is_smoke or values[0] == "init") and startup_args.startup_report is None:
            raise ValueError("ingestion initialization requires --startup-report")
        commands = ConfiguredIngestion(expected_modules=expected,
                                       startup_report=startup_args.startup_report)
        if smoke_args is None:
            return ingestion_main(values, commands=commands)
        print(canonical(smoke(smoke_args.output, commands=commands)))
        return 0
    except (OSError, ValueError) as error:
        print(canonical({"status": "error", "error": str(error)}))
        return 2


def _smoke_arguments(values: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kernelcat-worker ingest smoke")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(values)
