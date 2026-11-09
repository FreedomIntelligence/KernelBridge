"""Finite cross-owner capture and fence delivery, outside both owners' transactions."""
from __future__ import annotations

from pathlib import Path
from typing import cast

import kernelcat_executor
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.knowledge_fences import MULTI_TRIAL_HEADER
from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, canonical, digest
from kernelcat_executor.contracts.runtime import validate_runtime
from kernelcat_executor.operations.runtime import RuntimeOwner
from kernelcat_executor.state.runtime import RuntimeJournal
from kernelcat_knowledge import build_application
from kernelcat_knowledge.api.application import WikiApplication
from kernelcat_knowledge.contracts.runtime import validate_runtime as validate_knowledge
from kernelcat_knowledge.generated.knowledge_runtime import (
    KnowledgeCaptureRequest,
    KnowledgeConsumerStatus,
    KnowledgeFenceReceipt,
    KnowledgeFenceRequest,
    KnowledgeViewReference,
)

from kernelcat_worker.bindings import _knowledge_root
from kernelcat_worker.configuration import Config
from kernelcat_worker.knowledge import KnowledgeReadSurface
from kernelcat_worker.startup import PythonStartup


class ManagedKnowledge:
    def __init__(self, config: Config, base: Path, journal: RuntimeJournal,
                 artifacts: RuntimeArtifacts, *, startup: PythonStartup) -> None:
        config = startup.bind_config(config)
        self.journal, self.artifacts = journal, artifacts
        root = _knowledge_root(config, base)
        if root is not None:
            agent = (base / config.pi_worker.root).resolve()
            checkout = agent.parent.parent
            executor = Path(kernelcat_executor.__file__).resolve().parent
            protected = (agent, *(base / value for value in config.skill_roots),
                checkout / "contracts", checkout / "wiki/910c", checkout / "wiki/assets",
                executor / "contracts", executor / "evaluation")
            for path in protected:
                path = path.resolve()
                if root.is_relative_to(path) or path.is_relative_to(root):
                    raise ValueError("managed Knowledge root overlaps protected controller/evaluator/contract locations")
        self.application: WikiApplication | None = None if root is None else build_application(
            root, expected_modules=startup.knowledge_expected)
        if journal.path.exists() and journal.path.stat().st_size:
            self._account()

    def _account(self) -> Json:
        rows = self.journal.events()
        if not rows:
            raise Conflict("managed Knowledge requires an existing explicit runtime account")
        header = rows[0]
        account: Json = header["account"]
        pin = account.get("knowledge_authority_id")
        if self.application is not None and (header["kind"] != MULTI_TRIAL_HEADER or pin is None):
            raise Conflict("managed Knowledge requires a fresh v3 account with a pinned authority")
        if header["kind"] == MULTI_TRIAL_HEADER and pin is not None and self.application is None:
            raise Conflict("pinned v3 Knowledge authority requires its trusted configured root")
        return account

    def verify_root(self, run_id: str, trial_id: str) -> None:
        self.journal.knowledge_request(run_id, trial_id)
        account = self._account()
        if self.application is not None:
            self._authority(self.application, account["knowledge_authority_id"])

    def validate_creation(self, request: Json) -> None:
        if self.application is None:
            if request.get("multi_trial") and "knowledge_authority_id" in request:
                raise Conflict("pinned v3 Knowledge authority requires its trusted configured root")
            return
        if not request.get("multi_trial") or "knowledge_authority_id" not in request:
            raise Conflict("managed Knowledge requires a fresh v3 account with a pinned authority")
        self._authority(self.application, request["knowledge_authority_id"])

    @staticmethod
    def _authority(application: WikiApplication, expected: str) -> Json:
        status: Json = dict(validate_knowledge(application.control_status(), "KnowledgeControlStatus"))
        if status["authority_id"] != expected:
            raise Conflict("configured Knowledge root differs from the account's pinned authority")
        return status

    def install(self, owner: RuntimeOwner, run_id: str, trial_id: str, prepared: Json) -> None:
        # Original account/control CAS values belong to the full admitted envelope.
        # In particular, never observe a newer control revision before this lookup.
        envelope = owner.knowledge_request(run_id, trial_id)
        account = self._account()
        application = self.application
        if application is None:
            return
        status = self._authority(application, account["knowledge_authority_id"])
        if envelope is None:
            envelope = self._capture_envelope(account, run_id, trial_id, prepared, status["control_revision"])
        envelope = owner.prepare_knowledge(dict(validate_runtime(envelope, "PrepareKnowledgeCaptureRequest")))
        capture = cast(KnowledgeCaptureRequest, envelope["capture_request"])
        receipt = validate_knowledge(application.capture_consumer(capture), "KnowledgeCaptureReceipt")
        self.require_eligible(self._reconcile(application, capture, 100))
        application.open_view(receipt["view"])
        reference = self.artifacts.put(canonical(receipt).encode("utf-8"), "knowledge-capture")
        owner.install_knowledge({"preparation_intent_id": envelope["preparation_intent_id"],
            "capture_request_digest": receipt["registration"]["capture_request_digest"],
            "registration": receipt["registration"], "view": receipt["view"],
            "capture_receipt_ref": reference})

    @staticmethod
    def _capture_envelope(account: Json, run_id: str, trial_id: str, prepared: Json,
                          control_revision: int) -> Json:
        intent = (prepared["intent_id"] if prepared["schema_version"] == "kernelcat.prepared-run/v1"
                  else prepared["request"]["intent_id"])
        identity = {"preparation_intent_id": intent, "run_id": run_id, "trial_id": trial_id,
            "account_id": account["account_id"], "journal_authority_digest": account["authority_digest"]}
        consumer = {"consumer_id": "knowledge-consumer/" + digest(identity),
            "journal_authority_digest": account["authority_digest"], "account_id": account["account_id"],
            "run_id": run_id, "trial_id": trial_id, "required_journal_header": MULTI_TRIAL_HEADER}
        return {"preparation_intent_id": intent, "expected_account_revision": prepared["account"]["revision"],
            "capture_request": {"schema_version": "kernelcat.knowledge-capture-request/v1",
                "intent_id": "knowledge-capture/" + digest(identity), "consumer": consumer,
                "expected_control_revision": control_revision}}

    def _deliver(self, request: KnowledgeFenceRequest) -> KnowledgeFenceReceipt:
        return self.journal.knowledge_fence(dict(request))

    def _reconcile(self, application: WikiApplication, request: KnowledgeCaptureRequest,
                   limit: int) -> KnowledgeConsumerStatus:
        return validate_knowledge(application.reconcile_consumer(request, self._deliver, limit=limit),
                                  "KnowledgeConsumerStatus")

    def sync(self, run_id: str, trial_id: str, *, limit: int = 100) -> KnowledgeConsumerStatus:
        envelope = self.journal.knowledge_request(run_id, trial_id)
        account = self._account()
        if envelope is None:
            raise Conflict("Knowledge sync has no original captured request for this Run/Trial")
        application = self.application
        if application is None:
            raise Conflict("Knowledge sync requires the trusted configured root")
        self._authority(application, account["knowledge_authority_id"])
        return self._reconcile(application, cast(KnowledgeCaptureRequest, envelope["capture_request"]), limit)

    @staticmethod
    def require_eligible(status: KnowledgeConsumerStatus) -> None:
        if status["invalidated"]:
            raise Conflict("Knowledge dependency permanently invalidated for this Trial")
        if status["pending_fence_count"]:
            raise InDoubt("Knowledge consumer has pending original fence acknowledgements")

    def before_effect(self, run_id: str, trial_id: str) -> None:
        if self.application is not None:
            self.require_eligible(self.sync(run_id, trial_id))

    def open(self, binding: Json | None) -> KnowledgeReadSurface | None:
        if self.application is None:
            if binding is not None:
                raise Conflict("installed Knowledge binding has no trusted configured root")
            return None
        if binding is None:
            raise Conflict("managed Knowledge requires an installed retained binding")
        return self.application.open_view(cast(KnowledgeViewReference, binding["view"]))
