"""Agent-native control plane for KernelCat's CANNBench 910C campaign."""

from .evidence import (
    AttributionReport,
    EvidenceAssessment,
    GateReceipt,
    assess_evidence,
    attribute_carrier_score,
    evaluate_promotion,
)
from .execution import CommandRequest, CommandResult, CommandRunner, LegacyToolAdapter, UnifiedEvaluationPlan
from .formal import HostProofBundle, ProofObligation, ProofResult, bundle_host_proof
from .models import (
    BuildIdentity,
    Candidate,
    CarrierBundle,
    ContractError,
    EnvironmentFingerprint,
    EvidenceKind,
    EvidenceRecord,
    EvidenceStatus,
    ExperimentPhase,
    ExperimentPlan,
    OperatorId,
    ParentKind,
    ParentRef,
    ParentSet,
    Policy,
    ScoreSnapshot,
    canonical_json,
    content_id,
)
from .profiler import EventABDecision, PMUMechanismView, build_pmu_view, compare_event_samples
from .store import ArtifactRef, ContentAddressedStore, Ledger

__all__ = [
    "ArtifactRef",
    "AttributionReport",
    "BuildIdentity",
    "Candidate",
    "CarrierBundle",
    "CommandRequest",
    "CommandResult",
    "CommandRunner",
    "ContentAddressedStore",
    "ContractError",
    "EnvironmentFingerprint",
    "EventABDecision",
    "EvidenceAssessment",
    "EvidenceKind",
    "EvidenceRecord",
    "EvidenceStatus",
    "ExperimentPhase",
    "ExperimentPlan",
    "GateReceipt",
    "HostProofBundle",
    "Ledger",
    "LegacyToolAdapter",
    "OperatorId",
    "PMUMechanismView",
    "ParentKind",
    "ParentRef",
    "ParentSet",
    "Policy",
    "ProofObligation",
    "ProofResult",
    "ScoreSnapshot",
    "UnifiedEvaluationPlan",
    "assess_evidence",
    "attribute_carrier_score",
    "build_pmu_view",
    "bundle_host_proof",
    "canonical_json",
    "compare_event_samples",
    "content_id",
    "evaluate_promotion",
]

__version__ = "0.1.0"
