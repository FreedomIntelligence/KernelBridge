"""Coherent bounded selection and retention through the actual filesystem owners."""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from kernelcat_knowledge.cards.authority import (
    active_cards,
    card_owner_roots,
    card_roots,
)
from kernelcat_knowledge.contracts import (
    ContractError,
    canonical_json,
    validate_evidence_ref,
    validate_knowledge_card,
    validate_source_record,
)
from kernelcat_knowledge.contracts.runtime import dependency_key, wire_bytes
from kernelcat_knowledge.evidence.source_content import (
    LocalSourceFiles,
    bytes_digest,
    read_evidence_content,
    verify_source_content,
)
from kernelcat_knowledge.generated.knowledge_runtime import KnowledgeDependency
from kernelcat_knowledge.resources.binding import ResourceBinding
from kernelcat_knowledge.storage.artifacts import FilesystemArtifactResolver
from kernelcat_knowledge.storage.catalog_io import (
    catalog_locks,
    check_directory,
    read_bytes,
)
from kernelcat_knowledge.storage.filesystem import EvidenceCatalog
from kernelcat_knowledge.storage.retained import (
    MAX_ARTIFACT_BYTES,
    MAX_MEMBERS,
    MAX_TOTAL_BYTES,
    RetainedStore,
)
from kernelcat_knowledge.storage.sources import FilesystemSourceRepository


@contextmanager
def writer_boundary(
    root: Path, binding: ResourceBinding | None = None
) -> Iterator[None]:
    """Lock all selection owners in shared physical order, then control at commit."""
    owners = (
        binding.writer_roots
        if binding
        else [
            *card_owner_roots(card_roots(root, root / "wiki/cards")),
            root / "data/source-catalog",
            root / "data/evidence-catalog",
        ]
    )
    with catalog_locks(owners):
        yield


def dependency(kind: Any, record: Mapping[str, Any]) -> KnowledgeDependency:
    id_field = {
        "card_revision": "card_id",
        "source": "source_id",
        "evidence": "evidence_id",
    }[kind]
    revision = (
        validate_knowledge_card(record)["revision_id"]
        if kind == "card_revision"
        else None
    )
    return {
        "kind": kind,
        "object_id": record[id_field],
        "revision_id": revision,
        "content_digest": bytes_digest(canonical_json(record).encode("utf-8")),
    }


def select_metadata(
    root: Path, binding: ResourceBinding | None = None
) -> dict[str, Any]:
    """Called with writer_boundary held; never hashes or reads large raw sources."""
    cards = active_cards(
        binding.card_roots if binding else card_roots(root, root / "wiki/cards")
    )
    sources = FilesystemSourceRepository(
        binding.paths.source_records_dir if binding else root / "data/source-catalog"
    )
    evidence = EvidenceCatalog(root / "data/evidence-catalog")
    selection: dict[str, Any] = {"cards": cards, "sources": {}, "evidence": {}}
    state = {"members": 0, "bytes": 0}
    selected_ids = {
        value
        for card in cards
        for value in (card["card_id"], validate_knowledge_card(card)["revision_id"])
    }
    for card in cards:
        _count_record(card, state)
        if len(card.get("depends_on", [])) > MAX_MEMBERS:
            raise ContractError("retained card exceeds dependency edge limit")
        if set(card.get("depends_on", [])) - selected_ids:
            raise ContractError(
                "retained card dependency is outside the active V1 closure"
            )
        for evidence_id in _card_evidence_refs(card):
            _select_evidence(evidence_id, evidence, sources, selection, state)
    descriptors = [dependency("card_revision", card) for card in cards]
    for kind in ("source", "evidence"):
        descriptors.extend(
            dependency(kind, row)
            for row in selection[kind + "s" if kind == "source" else kind].values()
        )
    selection["dependencies"] = sorted(descriptors, key=dependency_key)
    selection["record_bytes"] = state["bytes"]
    if binding is not None:
        selection["resource_config"] = binding.descriptor
    return selection


def _card_evidence_refs(card: dict[str, Any]) -> list[str]:
    references = list(card.get("evidence_refs", []))
    if len(references) > MAX_MEMBERS:
        raise ContractError("retained card exceeds evidence reference limit")
    for claim in card.get("claims", []):
        if len(references) + len(claim["evidence_refs"]) > MAX_MEMBERS:
            raise ContractError("retained card exceeds evidence reference limit")
        references.extend(claim["evidence_refs"])
    return references


def apply_deny_overlay(selection: dict[str, Any], denied: set[str]) -> dict[str, Any]:
    """Make only a fresh selection; never rewrite a persisted selection or card."""
    if not denied:
        return selection
    denied_ids = {
        (member["kind"], member["object_id"])
        for member in selection["dependencies"]
        if wire_bytes(member).decode("utf-8") in denied
    }
    blocked = blocked_cards(selection, denied_ids)
    cards = [card for card in selection["cards"] if card["card_id"] not in blocked]
    evidence_ids = {
        identifier for card in cards for identifier in _card_evidence_refs(card)
    }
    evidence = {
        key: row for key, row in selection["evidence"].items() if key in evidence_ids
    }
    source_ids = {row["source_id"] for row in evidence.values()}
    sources = {
        key: row for key, row in selection["sources"].items() if key in source_ids
    }
    membership = (
        {("card_revision", card["card_id"]) for card in cards}
        | {("evidence", key) for key in evidence}
        | {("source", key) for key in sources}
    )
    records = [*cards, *evidence.values(), *sources.values()]
    return {
        **(
            {"resource_config": selection["resource_config"]}
            if "resource_config" in selection
            else {}
        ),
        "cards": cards,
        "sources": sources,
        "evidence": evidence,
        "dependencies": [
            member
            for member in selection["dependencies"]
            if (member["kind"], member["object_id"]) in membership
        ],
        "record_bytes": sum(
            len(canonical_json(record).encode("utf-8")) for record in records
        ),
    }


def blocked_cards(
    selection: dict[str, Any], denied_ids: set[tuple[str, str]]
) -> set[str]:
    cards = {card["card_id"]: card for card in selection["cards"]}
    revisions = {
        member["revision_id"]: member["object_id"]
        for member in selection["dependencies"]
        if member["kind"] == "card_revision"
    }
    reverse: dict[str, list[str]] = {key: [] for key in cards}
    blocked = set()
    for card_id, card in cards.items():
        if ("card_revision", card_id) in denied_ids or any(
            ("evidence", reference) in denied_ids
            or (
                reference in selection["evidence"]
                and ("source", selection["evidence"][reference]["source_id"])
                in denied_ids
            )
            for reference in _card_evidence_refs(card)
        ):
            blocked.add(card_id)
        for parent in card.get("depends_on", []):
            parent_id = parent if parent in cards else revisions.get(parent)
            if parent_id is not None:
                reverse[parent_id].append(card_id)
    queue = deque(sorted(blocked, key=lambda key: key.encode("utf-8")))
    while queue:
        parent = queue.popleft()
        for child in reverse[parent]:
            if child not in blocked:
                blocked.add(child)
                queue.append(child)
    return blocked


def _select_evidence(
    evidence_id: str,
    evidence: EvidenceCatalog,
    sources: FilesystemSourceRepository,
    selection: dict[str, Any],
    state: dict[str, int],
) -> None:
    if evidence_id in selection["evidence"]:
        return
    _check_member_room(state)
    row = evidence.get(evidence_id, strict=True)
    if row is None:
        raise ContractError(f"retained evidence is unavailable: {evidence_id}")
    validate_evidence_ref(row)
    _count_record(row, state)
    selection["evidence"][evidence_id] = row
    _select_source(row["source_id"], sources, selection, state)


def _check_member_room(state: dict[str, int]) -> None:
    if state["members"] >= MAX_MEMBERS:
        raise ContractError("retained view exceeds 16384 dependency members")


def _count_record(record: Mapping[str, Any], state: dict[str, int]) -> None:
    _check_member_room(state)
    size = len(canonical_json(record).encode("utf-8"))
    if size > MAX_ARTIFACT_BYTES or state["bytes"] + size > MAX_TOTAL_BYTES:
        raise ContractError("retained records exceed byte limits")
    state["members"] += 1
    state["bytes"] += size


def _select_source(
    source_id: str,
    sources: FilesystemSourceRepository,
    selection: dict[str, Any],
    state: dict[str, int],
) -> None:
    if source_id in selection["sources"]:
        return
    _check_member_room(state)
    source = sources.read(source_id)
    if source is None:
        raise ContractError(f"retained source is unavailable: {source_id}")
    validate_source_record(source)
    selectors = source.get("content_selector")
    if isinstance(selectors, list) and len(selectors) > MAX_MEMBERS:
        raise ContractError("retained source exceeds selector count limit")
    _count_record(source, state)
    selection["sources"][source_id] = source


def canonical_path(locator: str, root: Path) -> Path:
    """Resolve retained identities lexically, with no current-root filesystem lookup."""
    parsed = urlsplit(locator)
    if (
        parsed.scheme not in {"", "file"}
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
        or "::" in locator
    ):
        raise ContractError("retained source has an unsupported local locator")
    path = Path(unquote(parsed.path))
    absolute = Path(os.path.abspath(path if path.is_absolute() else root / path))
    if not absolute.is_relative_to(root):
        raise ContractError("retained source path leaves its original root")
    return absolute


class CapturingFiles(LocalSourceFiles):
    def __init__(
        self,
        store: RetainedStore,
        record_bytes: int,
        descriptor: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(store.root)
        self.store = store
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.directories: dict[str, list[str]] = {}
        self._directory_status: dict[str, bool] = {}
        self.total_bytes = record_bytes
        self.resolver = FilesystemArtifactResolver(
            repo_root=store.root,
            cas_root=store.root
            / (
                descriptor["selectors"]["evidence_cas_dir"]
                if descriptor
                else "artifacts/cas"
            ),
        )

    def path(self, locator: str) -> Path:
        return canonical_path(locator, self.root)

    def _retain(self, key: str, path: Path) -> bytes:
        existing = self.artifacts.get(key)
        if existing is not None:
            return self.store.blob(existing["digest"], existing["bytes"])
        if len(self.artifacts) >= MAX_MEMBERS:
            raise ContractError("retained view exceeds artifact count limit")
        remaining = min(MAX_ARTIFACT_BYTES, MAX_TOTAL_BYTES - self.total_bytes)
        raw = read_bytes(path, remaining)
        descriptor = {"key": key, "digest": self.store.put_blob(raw), "bytes": len(raw)}
        self.artifacts[key] = descriptor
        self.total_bytes += len(raw)
        return raw

    def read(self, path: Path) -> bytes:
        return self._retain("path:" + path.relative_to(self.root).as_posix(), path)

    def artifact(self, reference: str) -> bytes:
        return self._retain("ref:" + reference, self.resolver.resolve_path(reference))

    def is_directory(self, path: Path) -> bool:
        key = path.relative_to(self.root).as_posix()
        if key not in self._directory_status:
            self._directory_status[key] = path.is_dir()
            if self._directory_status[key]:
                check_directory(path)
        return self._directory_status[key]

    def directory_membership(self) -> dict[str, list[str]]:
        return {
            key: self.directories.get(key, [])
            for key, value in sorted(self._directory_status.items())
            if value
        }

    def members(self, path: Path) -> Iterator[Path]:
        key = path.relative_to(self.root).as_posix()
        if key in self.directories:
            yield from (self.root / member for member in self.directories[key])
            return
        members: list[Path] = []
        queue = [path]
        count = 0
        while queue:
            with os.scandir(queue.pop()) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_MEMBERS:
                        raise ContractError(
                            "retained source directory exceeds entry limit"
                        )
                    if entry.is_symlink():
                        raise ContractError(
                            "retained directory cannot contain symlinks"
                        )
                    member = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        queue.append(member)
                    elif entry.is_file(follow_symlinks=False):
                        members.append(member)
                    else:
                        raise ContractError(
                            "retained directory contains a nonregular member"
                        )
        self.directories[key] = [
            member.relative_to(self.root).as_posix() for member in sorted(members)
        ]
        yield from sorted(members)


def retain_selection(selection: dict[str, Any], store: RetainedStore) -> dict[str, Any]:
    from kernelcat_knowledge.resources.binding import validate_descriptor

    descriptor = (
        validate_descriptor(selection["resource_config"])
        if "resource_config" in selection
        else None
    )
    files = CapturingFiles(store, selection["record_bytes"], descriptor)
    for source in selection["sources"].values():
        verify_source_content(
            source, store.root, artifact_reader=files.artifact, files=files
        )
    for evidence in selection["evidence"].values():
        read_evidence_content(
            evidence,
            selection["sources"][evidence["source_id"]],
            store.root,
            max_lines=400,
            max_bytes=131072,
            artifact_reader=files.artifact,
            files=files,
        )
    records = _retain_records(selection, store)
    return {
        "schema_version": "kernelcat.retained-manifest/v2"
        if descriptor
        else "kernelcat.retained-manifest/v1",
        **({"resource_config": descriptor} if descriptor else {}),
        "authority_id": store.authority,
        "selection_rule": "active-v1-closure/v1",
        "dependencies": selection["dependencies"],
        "records": sorted(records, key=lambda row: dependency_key(row["dependency"])),
        "artifacts": [files.artifacts[key] for key in sorted(files.artifacts)],
        "directories": files.directory_membership(),
        "total_bytes": files.total_bytes,
    }


def _retain_records(
    selection: dict[str, Any], store: RetainedStore
) -> list[dict[str, Any]]:
    records = []
    for kind, values in (
        ("card_revision", selection["cards"]),
        ("source", selection["sources"].values()),
        ("evidence", selection["evidence"].values()),
    ):
        for record in values:
            content = canonical_json(record).encode("utf-8")
            records.append(
                {
                    "dependency": dependency(kind, record),
                    "digest": store.put_blob(content),
                    "bytes": len(content),
                }
            )
    return records


def known_dependency(
    root: Path, target: KnowledgeDependency, binding: ResourceBinding | None = None
) -> bool:
    kind = target["kind"]
    if kind == "source":
        record = FilesystemSourceRepository(
            binding.paths.source_records_dir
            if binding
            else root / "data/source-catalog"
        ).read(target["object_id"])
    elif kind == "evidence":
        record = EvidenceCatalog(root / "data/evidence-catalog").get(
            target["object_id"], strict=True
        )
    else:
        cards = active_cards(
            binding.card_roots if binding else card_roots(root, root / "wiki/cards")
        )
        record = next(
            (row for row in cards if row["card_id"] == target["object_id"]), None
        )
    return record is not None and dependency(kind, record) == target


def live_metadata(
    root: Path,
    binding: ResourceBinding,
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve exact dependencies of already validated, isolated query inputs.

    Native loader snapshots carry original record hashes. Do not rescan cards here:
    a new publication must not join the query after its deny decision. Capture keeps
    its distinct strict closure validation in select_metadata.
    """
    sources = FilesystemSourceRepository(binding.paths.source_records_dir)
    evidence = EvidenceCatalog(root / "data/evidence-catalog")
    selected = {"cards": cards, "sources": {}, "evidence": {}}
    for card in cards:
        for key in _card_evidence_refs(card):
            if key in selected["evidence"]:
                continue
            item = evidence.get(key, strict=True)
            if item is None:
                continue
            selected["evidence"][key] = item
            source = sources.read(item["source_id"])
            if source is not None:
                selected["sources"][item["source_id"]] = source
    # Legacy projections have no V1 authority record to retract. Their source,
    # evidence and parent edges still participate in the existing propagation rule.
    selected["dependencies"] = [
        {
            "kind": "card_revision",
            "object_id": card["card_id"],
            "revision_id": card["revision_id"],
            "content_digest": card["_authority_content_digest"],
        }
        for card in cards
        if "_authority_content_digest" in card
    ]
    for kind, records in [
        ("source", selected["sources"]),
        ("evidence", selected["evidence"]),
    ]:
        selected["dependencies"].extend(
            dependency(kind, row) for row in records.values()
        )
    return selected
