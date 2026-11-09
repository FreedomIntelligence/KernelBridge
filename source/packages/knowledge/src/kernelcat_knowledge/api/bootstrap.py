"""Explicit composition of the shared Python/CLI/MCP Knowledge application."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kernelcat_knowledge.generated.python_startup import (
    AdmittedModules,
    ExpectedPythonModules,
    ModuleAdmissionError,
    admit_modules,
    normalize_expected,
    read_expected,
)
from kernelcat_knowledge.resources.configuration import (
    configured_resource_root,
    discover_repo_root,
)

if TYPE_CHECKING:
    from kernelcat_knowledge.api.application import WikiApplication


def build_application(
    repo_root: Path | str | None = None,
    *,
    expected_modules: ExpectedPythonModules,
    knowledge: Any | None = None,
    read_model: Any | None = None,
) -> WikiApplication:
    admission = admit_modules(expected_modules, ("kernelcat_knowledge",))
    return _build_admitted_application(
        repo_root, admission=admission, knowledge=knowledge, read_model=read_model,
    )


def require_knowledge_admission(admission: AdmittedModules) -> None:
    """Accept only an observed process-local Knowledge result, never report JSON."""
    if type(admission) is not AdmittedModules:
        raise ModuleAdmissionError("Knowledge requires a checked admission value")
    names = [item["module_name"] for item in admission.report["observations"]]
    if names != ["kernelcat_knowledge"]:
        raise ModuleAdmissionError("Knowledge requires exactly its own module admission")
    admission.verify_loaded()


def _build_admitted_application(
    repo_root: Path | str | None, *, admission: AdmittedModules,
    knowledge: Any | None = None, read_model: Any | None = None,
) -> WikiApplication:
    require_knowledge_admission(admission)
    # Keep concrete repositories lazy so importing the public API remains inert.
    from kernelcat_knowledge.api.application import WikiApplication
    from kernelcat_knowledge.storage.artifacts import FilesystemArtifactResolver
    from kernelcat_knowledge.storage.filesystem import (
        EvidenceCatalog,
        IndexLoader,
        PageReader,
        RawReader,
        SourceCatalog,
        TrajectoryOutboxStore,
        WikiPaths,
    )
    from kernelcat_knowledge.storage.index import SQLiteReadModel

    # Explicit data evidence roots have no engine source checkout. Never fall
    # back to packaged knowledge when the caller chose one of these roots.
    explicit = Path(repo_root).resolve() if repo_root is not None else None
    marker = (
        explicit / ".kernelcat-evidence-root.json" if explicit is not None else None
    )
    if marker is not None and marker.is_file():
        if marker.read_bytes() != b'{"schema_version":"kernelcat.evidence-root/v1"}':
            raise ValueError("invalid external evidence root marker")
        root = configured_resource_root(explicit)
    else:
        root = discover_repo_root(repo_root)
    from kernelcat_knowledge.resources.binding import ResourceBinding

    binding = ResourceBinding.compose(root)
    paths = WikiPaths.from_binding(binding)
    selected = binding.paths
    repository = (
        knowledge
        if knowledge is not None
        else IndexLoader(
            repo_root=root,
            static_path=selected.static_index_path,
            dynamic_path=selected.dynamic_index_path,
            card_path=selected.card_index_path,
            rule_path=selected.rule_index_path,
        )
    )
    resolver = FilesystemArtifactResolver(
        repo_root=root, cas_root=selected.evidence_cas_dir
    )
    sources = SourceCatalog(
        paths.source_catalog, repo_root=root, artifact_resolver=resolver
    )
    evidence = EvidenceCatalog(paths.evidence_catalog)
    outbox = TrajectoryOutboxStore(paths.trajectory_delta_dir)
    sqlite = read_model or SQLiteReadModel(paths.sqlite_path)
    pages = PageReader(
        repository,
        wiki_root=paths.wiki_root,
        nav_index_path=paths.nav_index,
        repo_root=root,
    )
    raw = RawReader(
        raw_root=paths.raw_root,
        repo_root=root,
        loader=repository,
        artifact_resolver=resolver,
        max_lines=selected.raw_max_lines,
        max_bytes=selected.raw_max_bytes,
    )
    return WikiApplication(
        admission=admission,
        repo_root=root,
        binding=binding,
        knowledge=repository,
        sources=sources,
        evidence=evidence,
        outbox=outbox,
        read_model=sqlite,
        page_reader=pages,
        raw_reader=raw,
        card_root=paths.card_root,
        retained_inputs_supported=knowledge is None,
        card_snapshot=repository.card_snapshot if knowledge is None else None,
        artifact_reader=resolver.read_bytes,
    )


@lru_cache(maxsize=4)
def _configured_application(resource_root: str, admission: AdmittedModules) -> WikiApplication:
    return _build_admitted_application(resource_root, admission=admission)


def get_application(
    repo_root: Path | str | None = None, *, expected_modules: ExpectedPythonModules,
) -> WikiApplication:
    # Admission always precedes lookup; the frozen admission includes normalized
    # expected identity and actual origins. Cache storage is bounded to four roots.
    admission = admit_modules(expected_modules, ("kernelcat_knowledge",))
    return _configured_application(str(configured_resource_root(repo_root)), admission)


def standalone_application(
    repo_root: Path | str | None, *, expected_modules: object | None,
    binding_path: Path | None, startup_report: Path,
) -> WikiApplication:
    """Resolve explicit startup input and persist admission before business stores."""
    if not startup_report.is_absolute():
        raise ModuleAdmissionError("--startup-report must be an absolute path")
    if binding_path is not None:
        if not binding_path.is_absolute():
            raise ModuleAdmissionError("--expected-python-modules must be an absolute path")
        supplied = normalize_expected(read_expected(binding_path), ("kernelcat_knowledge",))
        if expected_modules is not None:
            trusted = normalize_expected(expected_modules, ("kernelcat_knowledge",))
            if supplied != trusted:
                raise ModuleAdmissionError("startup module bindings disagree")
        expected_modules = supplied
    if expected_modules is None:
        raise ModuleAdmissionError("explicit expected Python modules are required")
    admission = admit_modules(expected_modules, ("kernelcat_knowledge",))
    admission.export(startup_report)
    return _build_admitted_application(repo_root, admission=admission)
