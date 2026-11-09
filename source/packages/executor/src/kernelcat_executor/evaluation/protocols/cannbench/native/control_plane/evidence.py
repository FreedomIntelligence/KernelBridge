"""Evidence eligibility, score attribution, and fail-closed promotion gates."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import (
    Candidate,
    CarrierBundle,
    ContractError,
    EvidenceKind,
    EvidenceRecord,
    EvidenceStatus,
    ScoreSnapshot,
    as_payload,
    content_id,
)


@dataclass(frozen=True)
class EvidenceAssessment:
    evidence_id: str
    role: str
    decision_eligible: bool
    accepted: bool
    reasons: tuple[str, ...]
    facts: Mapping[str, Any] = field(default_factory=dict)

    def compact(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "role": self.role,
            "decision_eligible": self.decision_eligible,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "facts": dict(self.facts),
        }


def assess_evidence(record: EvidenceRecord, *, min_event_samples: int = 10) -> EvidenceAssessment:
    payload = record.payload
    reasons: list[str] = []

    if record.kind is EvidenceKind.PMU:
        return EvidenceAssessment(
            evidence_id=record.evidence_id,
            role="mechanism",
            decision_eligible=False,
            accepted=record.status in {EvidenceStatus.PASS, EvidenceStatus.PARTIAL},
            reasons=("PMU explains a mechanism but never decides latency or score promotion",),
            facts={"duration_excluded": payload.get("duration_excluded", True)},
        )

    if record.kind is EvidenceKind.LEGACY_DERIVED:
        return EvidenceAssessment(
            evidence_id=record.evidence_id,
            role="audit_only",
            decision_eligible=False,
            accepted=False,
            reasons=("legacy text/trajectory remains derived history, not verified evidence",),
            facts={"legacy_action": payload.get("legacy_action") or payload.get("action")},
        )

    if record.status is not EvidenceStatus.PASS:
        reasons.append(f"evidence status is {record.status.value}, not pass")

    if record.kind is EvidenceKind.EVENT_AB:
        if payload.get("profiler_enabled") is not False:
            reasons.append("Event A/B requires explicit profiler_enabled=false")
        if payload.get("same_device") is not True:
            reasons.append("base and candidate must run on the same device")
        if payload.get("correctness_pass") is not True:
            reasons.append("correctness must pass before Event A/B")
        base_n = int(payload.get("base_sample_count") or payload.get("sample_count") or 0)
        candidate_n = int(payload.get("candidate_sample_count") or payload.get("sample_count") or 0)
        if min(base_n, candidate_n) < min_event_samples:
            reasons.append(f"need >= {min_event_samples} samples per arm")
        lcb = payload.get("speedup_lcb95")
        if not isinstance(lcb, (int, float)) or isinstance(lcb, bool) or lcb <= 1.0:
            reasons.append("bootstrap 95% speedup lower bound must be > 1.0")
        if not str(payload.get("base_build_id", "")).startswith("build_"):
            reasons.append("base build identity missing")
        if not str(payload.get("candidate_build_id", "")).startswith("build_"):
            reasons.append("candidate build identity missing")
        facts = {
            "speedup_p50": payload.get("speedup_p50"),
            "speedup_lcb95": lcb,
            "aggregate_delta": payload.get("aggregate_delta"),
            "case_coverage": payload.get("case_coverage"),
            "base_sample_count": base_n,
            "candidate_sample_count": candidate_n,
        }
        return EvidenceAssessment(record.evidence_id, "performance", not reasons, not reasons, tuple(reasons), facts)

    if record.kind in {EvidenceKind.HOST_PROOF, EvidenceKind.COMPILE, EvidenceKind.CORRECTNESS, EvidenceKind.FORMAL}:
        if record.kind is EvidenceKind.CORRECTNESS:
            if not payload.get("scope"):
                reasons.append("correctness scope must be explicit")
            if payload.get("mismatches") not in {0, None}:
                reasons.append("correctness evidence reports mismatches")
        if record.kind is EvidenceKind.COMPILE and not str(payload.get("build_id", "")).startswith("build_"):
            reasons.append("compile evidence must bind a build_id")
        if record.kind is EvidenceKind.FORMAL and payload.get("claim_status") not in {"proved", "bounded", "host_checked"}:
            reasons.append("formal claim status is not recognized")
        facts = {
            key: payload.get(key)
            for key in ("scope", "build_id", "claim_status", "mismatches")
            if key in payload
        }
        return EvidenceAssessment(record.evidence_id, "correctness", not reasons, not reasons, tuple(reasons), facts)

    if record.kind in {EvidenceKind.OFFICIAL_STANDARD, EvidenceKind.OFFICIAL_HIDDEN}:
        if not payload.get("submission_id") or not payload.get("job_id"):
            reasons.append("official evidence requires submission_id and job_id")
        if payload.get("terminal") is not True:
            reasons.append("official job is not terminal")
        if payload.get("hard_failure") is True:
            reasons.append("official job ended in hard_failure")
        return EvidenceAssessment(record.evidence_id, "official_run", not reasons, not reasons, tuple(reasons), payload)

    if record.kind is EvidenceKind.SCORE_SNAPSHOT:
        if not payload.get("snapshot_id"):
            reasons.append("score snapshot ID missing")
        return EvidenceAssessment(record.evidence_id, "score_snapshot", not reasons, not reasons, tuple(reasons), payload)

    return EvidenceAssessment(
        record.evidence_id,
        "unknown",
        False,
        False,
        (f"unsupported evidence kind: {record.kind.value}",),
        {},
    )


@dataclass(frozen=True)
class PromotionCheck:
    name: str
    passed: bool
    evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    def compact(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": "pass" if self.passed else "fail",
            "evidence_ids": list(self.evidence_ids),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateReceipt:
    candidate_id: str
    current_decision_parent_id: str
    checks: tuple[PromotionCheck, ...]
    eligible_official: bool
    pending_run_ids: tuple[str, ...]
    policy_id: str
    source_tree_sha256: str
    casepack_sha256: str

    @property
    def gate_id(self) -> str:
        return content_id("gate", self)

    def compact(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "candidate_id": self.candidate_id,
            "decision": "promote" if self.eligible_official else "reject",
            "eligible_official": self.eligible_official,
            "checks": [check.compact() for check in self.checks],
            "pending_run_ids": list(self.pending_run_ids),
            "binding": {
                "current_decision_parent_id": self.current_decision_parent_id,
                "policy_id": self.policy_id,
                "source_tree_sha256": self.source_tree_sha256,
                "casepack_sha256": self.casepack_sha256,
            },
        }


def _matching(
    evidence: Iterable[EvidenceRecord],
    *,
    kind: EvidenceKind,
    candidate: Candidate,
    scope: str | None = None,
) -> list[EvidenceRecord]:
    result: list[EvidenceRecord] = []
    for record in evidence:
        if record.kind is not kind or record.candidate_id != candidate.candidate_id:
            continue
        if record.source_tree_sha256 != candidate.source_tree_sha256:
            continue
        if record.casepack_sha256 != candidate.environment.casepack_sha256:
            continue
        if scope is not None and record.payload.get("scope") != scope:
            continue
        result.append(record)
    return result


def evaluate_promotion(
    candidate: Candidate,
    evidence: Iterable[EvidenceRecord],
    *,
    current_decision_parent_id: str,
    pending_run_ids: Iterable[str] = (),
    require_formal: bool = False,
    require_pmu: bool = False,
) -> GateReceipt:
    """Evaluate the only new-layer gate allowed to authorize submission."""
    records = tuple(evidence)
    pending = tuple(sorted(set(pending_run_ids)))
    checks: list[PromotionCheck] = []

    current_parent = candidate.parents.decision_parent.parent_id == current_decision_parent_id
    checks.append(
        PromotionCheck(
            "current_parent",
            current_parent,
            detail="candidate is bound to current parent" if current_parent else "parent moved; rebase and rerun",
        )
    )

    def require(kind: EvidenceKind, name: str, scope: str | None = None) -> list[EvidenceRecord]:
        matches = _matching(records, kind=kind, candidate=candidate, scope=scope)
        assessments = [assess_evidence(record) for record in matches]
        passed = any(item.accepted and item.decision_eligible for item in assessments)
        detail = "; ".join(reason for item in assessments for reason in item.reasons)
        if not matches:
            detail = "no source/casepack-bound evidence"
        checks.append(PromotionCheck(name, passed, tuple(item.evidence_id for item in matches), detail))
        return matches

    require(EvidenceKind.HOST_PROOF, "host_proof")
    require(EvidenceKind.COMPILE, "compile")
    require(EvidenceKind.CORRECTNESS, "representative_exact", "representative")
    require(EvidenceKind.CORRECTNESS, "fallback_exact", "fallback")

    event_records = _matching(records, kind=EvidenceKind.EVENT_AB, candidate=candidate)
    full20 = [record for record in event_records if record.payload.get("case_coverage") == "full20"]
    full20_assessments = [assess_evidence(record) for record in full20]
    full20_pass = any(
        assessment.accepted
        and assessment.decision_eligible
        and isinstance(assessment.facts.get("aggregate_delta"), (int, float))
        and not isinstance(assessment.facts.get("aggregate_delta"), bool)
        and assessment.facts["aggregate_delta"] > 0
        for assessment in full20_assessments
    )
    checks.append(
        PromotionCheck(
            "same_card_full20",
            full20_pass,
            tuple(record.evidence_id for record in full20),
            "requires correctness-pass profiler-off Event A/B, LCB>1, and aggregate_delta>0",
        )
    )

    if require_formal:
        require(EvidenceKind.FORMAL, "formal_claims")
    else:
        formal = _matching(records, kind=EvidenceKind.FORMAL, candidate=candidate)
        checks.append(
            PromotionCheck(
                "formal_claims_optional",
                all(assess_evidence(item).accepted for item in formal),
                tuple(item.evidence_id for item in formal),
                "optional claims must be honest when present",
            )
        )

    pmu = _matching(records, kind=EvidenceKind.PMU, candidate=candidate)
    pmu_pass = bool(pmu) and any(assess_evidence(item).accepted for item in pmu)
    checks.append(
        PromotionCheck(
            "pmu_mechanism" if require_pmu else "pmu_mechanism_optional",
            pmu_pass if require_pmu else True,
            tuple(item.evidence_id for item in pmu),
            "mechanism evidence only; never used as latency proof",
        )
    )

    checks.append(PromotionCheck("no_pending_runs", not pending, pending, "all required runs must be terminal"))
    eligible = all(check.passed for check in checks)
    return GateReceipt(
        candidate_id=candidate.candidate_id,
        current_decision_parent_id=current_decision_parent_id,
        checks=tuple(checks),
        eligible_official=eligible,
        pending_run_ids=pending,
        policy_id=candidate.policy_id,
        source_tree_sha256=candidate.source_tree_sha256,
        casepack_sha256=candidate.environment.casepack_sha256,
    )


@dataclass(frozen=True)
class ComponentAttribution:
    component: str
    before: float | None
    after: float | None
    delta: float | None
    source_changed: bool
    attributable: bool
    reason: str

    def compact(self) -> dict[str, Any]:
        return as_payload(self)


@dataclass(frozen=True)
class AttributionReport:
    carrier_id: str
    submission_id: str
    before_snapshot_id: str
    after_snapshot_id: str
    carrier_total_delta: float
    components: tuple[ComponentAttribution, ...]
    unambiguous: bool

    @property
    def attribution_id(self) -> str:
        return content_id("attribution", self)

    def compact(self) -> dict[str, Any]:
        return {
            "attribution_id": self.attribution_id,
            "carrier_total_delta": self.carrier_total_delta,
            "unambiguous": self.unambiguous,
            "components": [component.compact() for component in self.components],
            "provenance": {
                "carrier_id": self.carrier_id,
                "submission_id": self.submission_id,
                "before_snapshot_id": self.before_snapshot_id,
                "after_snapshot_id": self.after_snapshot_id,
            },
        }


def attribute_carrier_score(
    before_bundle: CarrierBundle,
    after_bundle: CarrierBundle,
    before: ScoreSnapshot,
    after: ScoreSnapshot,
    *,
    submission_id: str,
) -> AttributionReport:
    """Attribute a bundled submission without multiplying its total gain by N."""
    if after_bundle.submission_id != submission_id:
        raise ContractError("submission is not linked to the after carrier bundle")
    if before.task_version != after.task_version or before.hardware != after.hardware:
        raise ContractError("score snapshots are not comparable")
    components = sorted(set(before_bundle.component_sources) | set(after_bundle.component_sources))
    changed = {
        component
        for component in components
        if before_bundle.component_sources.get(component) != after_bundle.component_sources.get(component)
    }
    rows: list[ComponentAttribution] = []
    for component in components:
        before_score = before.components.get(component)
        after_score = after.components.get(component)
        delta = None if before_score is None or after_score is None else after_score - before_score
        source_changed = component in changed
        if not source_changed:
            attributable = False
            reason = "component source did not change"
        elif delta is None:
            attributable = False
            reason = "component score row missing; carrier total is ambiguous"
        else:
            attributable = True
            reason = "source changed and linked component score moved"
        rows.append(ComponentAttribution(component, before_score, after_score, delta, source_changed, attributable, reason))
    unambiguous = bool(changed) and all(row.attributable for row in rows if row.source_changed)
    return AttributionReport(
        carrier_id=after_bundle.carrier_id,
        submission_id=submission_id,
        before_snapshot_id=before.snapshot_id,
        after_snapshot_id=after.snapshot_id,
        carrier_total_delta=after.total - before.total,
        components=tuple(rows),
        unambiguous=unambiguous,
    )
