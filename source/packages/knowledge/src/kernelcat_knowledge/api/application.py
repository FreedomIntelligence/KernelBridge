"""Python entry point shared by the CLI and MCP transports.

Each method delegates to its owning capability; this facade owns no alternative
source, publication or effect rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from kernelcat_knowledge.api.bootstrap import require_knowledge_admission
from kernelcat_knowledge.api.history import read_history, verify_history
from kernelcat_knowledge.cards import revisions
from kernelcat_knowledge.cards.active import load_cards
from kernelcat_knowledge.cards.eligibility import FactEligibility
from kernelcat_knowledge.contracts import ContractError, validate_contract
from kernelcat_knowledge.contracts.responses import response
from kernelcat_knowledge.evidence import catalog as evidence_catalog
from kernelcat_knowledge.evidence import read as evidence_read
from kernelcat_knowledge.evidence.trace import trace
from kernelcat_knowledge.generated.knowledge_runtime import (
    KnowledgeCaptureReceipt,
    KnowledgeCaptureRequest,
    KnowledgeConsumerStatus,
    KnowledgeControlStatus,
    KnowledgeFencePage,
    KnowledgeRetractionRequest,
    KnowledgeRetractionStatus,
    KnowledgeViewReference,
)
from kernelcat_knowledge.generated.python_startup import (
    AdmittedModules,
    PythonModuleReport,
)
from kernelcat_knowledge.ports import (
    EvidenceRepository,
    KnowledgeRepository,
    ReadModel,
    TrajectoryOutbox,
    WikiSourceRepository,
)
from kernelcat_knowledge.query import catalog as query_catalog
from kernelcat_knowledge.query import pages
from kernelcat_knowledge.query.identity import policy_digest, query_binding
from kernelcat_knowledge.resources.binding import ResourceBinding
from kernelcat_knowledge.retained.control import FenceDelivery, KnowledgeControl
from kernelcat_knowledge.retained.view import RetainedKnowledgeView
from kernelcat_knowledge.sources import catalog as source_catalog
from kernelcat_knowledge.storage import projections, snapshots
from kernelcat_knowledge.storage.retained import RetainedStore
from kernelcat_knowledge.trajectories import outbox


class WikiApplication:
    def __init__(
        self,
        *,
        admission: AdmittedModules,
        repo_root: Path,
        knowledge: KnowledgeRepository,
        sources: WikiSourceRepository,
        evidence: EvidenceRepository,
        outbox: TrajectoryOutbox,
        read_model: ReadModel,
        page_reader: Any,
        raw_reader: Any,
        card_root: Path | None,
        binding: ResourceBinding,
        card_snapshot: Callable[[], tuple[list[dict[str, Any]], str]] | None = None,
        retained_inputs_supported: bool = False,
        artifact_reader: Callable[[str], bytes] | None = None,
    ) -> None:
        require_knowledge_admission(admission)
        self._admission = admission
        self.repo_root = Path(repo_root).resolve()
        self.knowledge = knowledge
        self.sources = sources
        self.evidence = evidence
        self.outbox = outbox
        self.read_model = read_model
        self.page_reader = page_reader
        self.raw_reader = raw_reader
        self.card_root = card_root
        self.binding = binding
        self._card_snapshot = card_snapshot
        self._schema_digest = policy_digest()
        self._eligibility = FactEligibility(
            self.repo_root, sources.get, evidence.get, artifact_reader=artifact_reader
        )
        self._control = KnowledgeControl(
            RetainedStore(self.repo_root, binding=binding),
            schema_digest=self._schema_digest,
        )
        self._query_binding = query_binding(
            schema_digest=self._schema_digest,
            authority=self._control.store.authority
            if retained_inputs_supported
            else None,
            config_digest=binding.digest if retained_inputs_supported else None,
        )
        self._retained_inputs_supported = retained_inputs_supported

    @property
    def startup_report(self) -> PythonModuleReport:
        return self._admission.report

    def export_startup_report(self, destination: Path) -> None:
        self._admission.export(destination)

    def control_status(self) -> KnowledgeControlStatus:
        return self._control.control_status()

    def consumer_status(
        self, request: KnowledgeCaptureRequest
    ) -> KnowledgeConsumerStatus:
        return self._control.consumer_status(request)

    def reconcile_consumer(
        self,
        request: KnowledgeCaptureRequest,
        deliver_fence: FenceDelivery,
        limit: int = 100,
    ) -> KnowledgeConsumerStatus:
        return self._control.reconcile_consumer(request, deliver_fence, limit=limit)

    def capture_consumer(self, request: KnowledgeCaptureRequest) -> KnowledgeCaptureReceipt:
        if not self._retained_inputs_supported:
            raise ContractError(
                "retained capture requires native filesystem owner inputs; "
                "injected loaders are unsupported"
            )
        return self._control.capture_consumer(request)

    def open_view(self, reference: KnowledgeViewReference) -> RetainedKnowledgeView:
        return self._control.open_view(reference)

    def begin_retraction(
        self, request: KnowledgeRetractionRequest
    ) -> KnowledgeRetractionStatus:
        return self._control.begin_retraction(request)

    def retraction_status(self, intent_id: str) -> KnowledgeRetractionStatus:
        return self._control.retraction_status(intent_id)

    def pending_fences(
        self, intent_id: str, cursor: str | None = None, limit: int = 100
    ) -> KnowledgeFencePage:
        return self._control.pending_fences(intent_id, cursor=cursor, limit=limit)

    def reconcile_retraction(
        self, intent_id: str, deliver_fence: FenceDelivery, limit: int = 100
    ) -> KnowledgeRetractionStatus:
        return self._control.reconcile_retraction(intent_id, deliver_fence, limit=limit)

    def search(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return query_catalog.search(
            request,
            knowledge=self.knowledge,
            read_model=self.read_model,
            eligibility=self._eligibility,
            binding=self._query_binding,
            card_snapshot=self._card_snapshot,
            denial_filter=(
                self._control.denied_live_cards
                if self._retained_inputs_supported
                else None
            ),
        )

    def show(
        self, knowledge_id: str, *, view: str = "agent", max_chars: int = 12000
    ) -> dict[str, Any]:
        return pages.show(
            knowledge_id,
            knowledge=self.knowledge,
            page_reader=self.page_reader,
            view=view,
            max_chars=max_chars,
        )

    def get_page_legacy(self, knowledge_id: str) -> dict[str, Any]:
        return pages.get_page_legacy(knowledge_id, page_reader=self.page_reader)

    def source_register(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return source_catalog.source_register(record, sources=self.sources)

    def source_show(self, source_id: str) -> dict[str, Any]:
        return source_catalog.source_show(source_id, sources=self.sources)

    def source_list(self, *, source_class: str | None = None) -> dict[str, Any]:
        return source_catalog.source_list(
            sources=self.sources, source_class=source_class
        )

    def source_verify(self, source_id: str) -> dict[str, Any]:
        return source_catalog.source_verify(source_id, sources=self.sources)

    def evidence_register(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        return evidence_catalog.evidence_register(evidence, repository=self.evidence)

    def evidence_show(self, evidence_id: str) -> dict[str, Any]:
        return evidence_catalog.evidence_show(evidence_id, repository=self.evidence)

    def raw_grep(
        self,
        *,
        pattern: str,
        source_id: str | None = None,
        scope: str | None = None,
        context: int = 4,
        glob: str | None = None,
        include_tests: bool = False,
        max_hits: int = 30,
        legacy: bool = False,
    ) -> dict[str, Any]:
        return evidence_read.raw_grep(
            sources=self.sources,
            raw_reader=self.raw_reader,
            pattern=pattern,
            source_id=source_id,
            scope=scope,
            context=context,
            glob=glob,
            include_tests=include_tests,
            max_hits=max_hits,
            legacy=legacy,
        )

    def raw_read(
        self,
        *,
        path: str | None = None,
        evidence_id: str | None = None,
        start: int = 1,
        end: int | None = None,
        selector_override: Mapping[str, Any] | None = None,
        legacy: bool = False,
    ) -> dict[str, Any]:
        return evidence_read.raw_read(
            sources=self.sources,
            evidence_repository=self.evidence,
            raw_reader=self.raw_reader,
            path=path,
            evidence_id=evidence_id,
            start=start,
            end=end,
            selector_override=selector_override,
            legacy=legacy,
        )

    def trace(self, knowledge_id: str) -> dict[str, Any]:
        return trace(
            knowledge_id,
            knowledge=self.knowledge,
            sources=self.sources,
            evidence_repository=self.evidence,
        )

    def append_trajectory(
        self, delta: Mapping[str, Any], *, legacy: bool = False
    ) -> dict[str, Any]:
        return outbox.append_trajectory(delta, outbox=self.outbox, legacy=legacy)

    def outbox_status(self) -> dict[str, Any]:
        return outbox.outbox_status(outbox=self.outbox)

    def index_rebuild(self) -> dict[str, Any]:
        return projections.index_rebuild(
            knowledge=self.knowledge, read_model=self.read_model
        )

    def index_status(self) -> dict[str, Any]:
        return projections.index_status(
            knowledge=self.knowledge, read_model=self.read_model
        )

    def card_validate(self, card: Mapping[str, Any]) -> dict[str, Any]:
        return revisions.card_validate(card, eligibility=self._eligibility)

    def card_diff(self, card: Mapping[str, Any]) -> dict[str, Any]:
        return revisions.card_diff(card, knowledge=self.knowledge)

    def card_publish(
        self, card: Mapping[str, Any], *, reviewed: bool = False
    ) -> dict[str, Any]:
        self.binding.require_sealable()
        if self.card_root is None:
            raise ContractError(
                "card publication is disabled by resource configuration"
            )
        return revisions.card_publish(
            card,
            binding=self.binding,
            card_root=self.card_root,
            repo_root=self.repo_root,
            reviewed=reviewed,
            history_verifier=lambda normalized: verify_history(self, normalized),
        )

    def history_read(self, card_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        card = self.knowledge.find_by_id(card_id)
        if (card is not None and self._retained_inputs_supported
                and self._control.denied_live_cards([card])):
            raise ContractError("history card or source is currently retracted")
        return read_history(self, card_id, offset=offset, limit=limit)

    def validate_object(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_contract(payload)
        return response(
            "kernelcat.validation-result/v1",
            "valid",
            contract=normalized["schema_version"],
            object=normalized,
        )

    def snapshot_create(self, output: Path) -> dict[str, Any]:
        if self.card_root is None:
            raise ContractError("snapshot requires a configured card root")
        return snapshots.snapshot_create(
            output,
            knowledge=self.knowledge,
            sources=self.sources,
            evidence=self.evidence,
            card_root=self.card_root,
            repo_root=self.repo_root,
        )

    def doctor(self) -> dict[str, Any]:
        active = self.knowledge.load_all()
        cards, invalid = load_cards(self.knowledge)
        source_count = len(self.sources.list())
        evidence_count = (
            len(self.evidence.list()) if hasattr(self.evidence, "list") else 0
        )
        read_model = self.read_model.status(
            expected_revision=self.knowledge.active_index_revision
        )
        outbox = self.outbox.status()
        critical = bool(
            self.knowledge.load_errors
            or invalid
            or read_model.get("status") == "corrupt"
        )
        return response(
            "kernelcat.doctor/v1",
            "degraded" if critical else "ok",
            active_entry_count=len(active),
            active_card_count=len(cards),
            invalid_card_count=len(invalid),
            load_errors=list(self.knowledge.load_errors),
            source_count=source_count,
            evidence_count=evidence_count,
            read_model=read_model,
            outbox=outbox,
        )
