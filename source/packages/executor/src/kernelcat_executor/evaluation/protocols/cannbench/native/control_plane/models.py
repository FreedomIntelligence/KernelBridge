"""Immutable domain objects for the 910C campaign control plane.

Identity-bearing records are content addressed.  Timestamps, mutable working
paths, account names, and other machine-local details are deliberately excluded
from their IDs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_RE = re.compile(r"token|secret|password|cookie|authorization|credential", re.I)


class ContractError(ValueError):
    """Raised when a record would encode an ambiguous or unsafe fact."""


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value, key=lambda item: str(item)):
            text_key = str(key)
            if _SECRET_KEY_RE.search(text_key):
                raise ContractError(f"secret-shaped key is forbidden in durable payloads: {text_key}")
            result[text_key] = _plain(value[key])
        return result
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(item) for item in value), key=canonical_json)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("NaN and infinity are forbidden in durable payloads")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ContractError(f"unsupported durable payload type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_id(prefix: str, value: Any) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ContractError(f"invalid identity prefix: {prefix!r}")
    return f"{prefix}_{content_digest(value)}"


def require_sha256(value: str, field_name: str) -> str:
    normalized = str(value).lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ContractError(f"{field_name} must be a SHA-256 digest")
    return normalized


def _snake_case(name: str) -> str:
    text = str(name).strip().replace("-", "_").replace(" ", "_")
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    aliases = {
        "conv3_d_backprop_filter": "conv_3d_backprop_filter",
        "conv3d_backprop_filter": "conv_3d_backprop_filter",
        "grid_sampler3_d": "grid_sampler_3d",
        "grid_sampler3d": "grid_sampler_3d",
    }
    return aliases.get(text, text)


@dataclass(frozen=True, order=True)
class OperatorId:
    value: str

    def __post_init__(self) -> None:
        normalized = _snake_case(self.value)
        if not normalized or not re.fullmatch(r"[a-z][a-z0-9_]*", normalized):
            raise ContractError(f"invalid operator name: {self.value!r}")
        object.__setattr__(self, "value", normalized)

    @classmethod
    def parse(cls, value: str | "OperatorId") -> "OperatorId":
        return value if isinstance(value, cls) else cls(str(value))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class EnvironmentFingerprint:
    soc: str
    compute_unit: str
    device_isa: str
    cann_version: str
    task_version: str
    toolchain_sha256: str
    casepack_sha256: str

    def __post_init__(self) -> None:
        if self.soc.lower() != "910c":
            raise ContractError(f"910C control plane cannot record soc={self.soc!r}")
        object.__setattr__(self, "soc", "910c")
        object.__setattr__(self, "toolchain_sha256", require_sha256(self.toolchain_sha256, "toolchain_sha256"))
        object.__setattr__(self, "casepack_sha256", require_sha256(self.casepack_sha256, "casepack_sha256"))
        if not all((self.compute_unit, self.device_isa, self.cann_version, self.task_version)):
            raise ContractError("environment fingerprint fields must be explicit")

    @property
    def environment_id(self) -> str:
        return content_id("env", self)


class ParentKind(str, Enum):
    VERIFIED_SOURCE = "verified_source"
    CANDIDATE = "candidate"
    OFFICIAL_STANDARD = "official_standard"
    OFFICIAL_HIDDEN = "official_hidden"
    CARRIER = "carrier"


@dataclass(frozen=True)
class ParentRef:
    operator: OperatorId
    source_sha256: str
    environment_id: str
    kind: ParentKind = ParentKind.VERIFIED_SOURCE
    candidate_id: str | None = None
    submission_id: str | None = None
    score_snapshot_id: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", OperatorId.parse(self.operator))
        object.__setattr__(self, "source_sha256", require_sha256(self.source_sha256, "source_sha256"))
        if not self.environment_id.startswith("env_"):
            raise ContractError("environment_id must be content addressed")
        if self.kind is ParentKind.CANDIDATE and not self.candidate_id:
            raise ContractError("candidate parent requires candidate_id")
        if self.kind in {ParentKind.OFFICIAL_STANDARD, ParentKind.OFFICIAL_HIDDEN} and not self.submission_id:
            raise ContractError("official parent requires submission_id")

    @property
    def parent_id(self) -> str:
        return content_id(
            "parent",
            {
                "operator": self.operator.value,
                "source_sha256": self.source_sha256,
                "environment_id": self.environment_id,
                "kind": self.kind.value,
                "candidate_id": self.candidate_id,
                "submission_id": self.submission_id,
                "score_snapshot_id": self.score_snapshot_id,
            },
        )


@dataclass(frozen=True)
class ParentSet:
    """Split patch ancestry from the live parent that promotion must beat."""

    code_parent: ParentRef
    decision_parent: ParentRef
    carrier_parent: ParentRef | None = None

    def __post_init__(self) -> None:
        if self.code_parent.operator != self.decision_parent.operator:
            raise ContractError("code_parent and decision_parent must target the same operator")
        if self.code_parent.environment_id != self.decision_parent.environment_id:
            raise ContractError("parents from different environments cannot be mixed")
        if self.carrier_parent and self.carrier_parent.environment_id != self.code_parent.environment_id:
            raise ContractError("carrier parent environment must match")

    @property
    def parent_set_id(self) -> str:
        return content_id(
            "parents",
            {
                "code_parent": self.code_parent.parent_id,
                "decision_parent": self.decision_parent.parent_id,
                "carrier_parent": self.carrier_parent.parent_id if self.carrier_parent else None,
            },
        )


@dataclass(frozen=True)
class Policy:
    """One independently falsifiable optimization mechanism."""

    operator: OperatorId
    mechanism_id: str
    hypothesis: str
    applicability: tuple[str, ...]
    fallback: str
    expected_signals: Mapping[str, str] = field(default_factory=dict)
    touched_surfaces: tuple[str, ...] = field(default_factory=tuple)
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    co_mechanisms: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", OperatorId.parse(self.operator))
        if not re.fullmatch(r"[a-z][a-z0-9_.-]*", self.mechanism_id):
            raise ContractError("mechanism_id must be a stable atom-like identifier")
        if self.co_mechanisms:
            raise ContractError("one Policy may contain exactly one mechanism")
        if not self.hypothesis.strip() or not self.fallback.strip():
            raise ContractError("policy hypothesis and fallback are mandatory")
        if not self.applicability:
            raise ContractError("policy applicability cannot be empty")
        for name, direction in self.expected_signals.items():
            if direction not in {"increase", "decrease", "unchanged", "observe"}:
                raise ContractError(f"invalid expected direction for {name}: {direction}")

    @property
    def policy_id(self) -> str:
        return content_id("policy", self)


@dataclass(frozen=True)
class Candidate:
    operator: OperatorId
    parents: ParentSet
    policy_id: str
    source_tree_sha256: str
    task_sha256: str
    environment: EnvironmentFingerprint
    capability_predicate: str
    fallback_entry: str
    source_manifest_ref: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", OperatorId.parse(self.operator))
        object.__setattr__(self, "source_tree_sha256", require_sha256(self.source_tree_sha256, "source_tree_sha256"))
        object.__setattr__(self, "task_sha256", require_sha256(self.task_sha256, "task_sha256"))
        if self.operator != self.parents.code_parent.operator:
            raise ContractError("candidate operator must match its parent set")
        if self.environment.environment_id != self.parents.code_parent.environment_id:
            raise ContractError("candidate environment must match its parents")
        if not self.policy_id.startswith("policy_"):
            raise ContractError("candidate requires a content-addressed policy_id")
        if not self.capability_predicate.strip() or not self.fallback_entry.strip():
            raise ContractError("capability predicate and fallback entry are mandatory")

    @property
    def candidate_id(self) -> str:
        return content_id(
            "candidate",
            {
                "operator": self.operator.value,
                "parents": self.parents.parent_set_id,
                "policy_id": self.policy_id,
                "source_tree_sha256": self.source_tree_sha256,
                "task_sha256": self.task_sha256,
                "environment_id": self.environment.environment_id,
                "capability_predicate": self.capability_predicate,
                "fallback_entry": self.fallback_entry,
                "source_manifest_ref": self.source_manifest_ref,
                "labels": self.labels,
            },
        )


class ExperimentPhase(str, Enum):
    HOST_PROOF = "host_proof"
    COMPILE = "compile"
    CORRECTNESS = "correctness"
    EVENT_AB = "event_ab"
    PMU = "pmu"
    FORMAL = "formal"
    OFFICIAL_STANDARD = "official_standard"
    OFFICIAL_HIDDEN = "official_hidden"


@dataclass(frozen=True)
class BuildIdentity:
    source_tree_sha256: str
    task_sha256: str
    environment_id: str
    build_options: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_tree_sha256", require_sha256(self.source_tree_sha256, "source_tree_sha256"))
        object.__setattr__(self, "task_sha256", require_sha256(self.task_sha256, "task_sha256"))
        if not self.environment_id.startswith("env_"):
            raise ContractError("build identity requires environment_id")

    @property
    def build_id(self) -> str:
        return content_id("build", self)


@dataclass(frozen=True)
class ExperimentPlan:
    candidate_id: str
    decision_parent_id: str
    base_build_id: str
    candidate_build_id: str
    backend: str
    phases: tuple[ExperimentPhase, ...]
    case_ids: tuple[str, ...]
    repeat: int
    device_pool: tuple[str, ...] = field(default_factory=tuple)
    profiler_groups: tuple[str, ...] = field(default_factory=tuple)
    seed: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.candidate_id.startswith("candidate_"):
            raise ContractError("experiment requires candidate_id")
        if not self.decision_parent_id.startswith("parent_"):
            raise ContractError("experiment requires decision_parent_id")
        if not self.base_build_id.startswith("build_") or not self.candidate_build_id.startswith("build_"):
            raise ContractError("experiment must bind both compiled arms")
        if not self.phases:
            raise ContractError("experiment requires at least one phase")
        if self.repeat < 1:
            raise ContractError("repeat must be positive")
        if ExperimentPhase.EVENT_AB in self.phases and not self.case_ids:
            raise ContractError("Event A/B requires explicit cases")
        if ExperimentPhase.PMU in self.phases and ExperimentPhase.EVENT_AB not in self.phases:
            raise ContractError("PMU must attach to an Event A/B experiment")

    @property
    def experiment_id(self) -> str:
        return content_id("experiment", self)

    @property
    def idempotency_key(self) -> str:
        return content_id(
            "request",
            {
                "experiment_id": self.experiment_id,
                "backend": self.backend,
                "phases": [phase.value for phase in self.phases],
                "case_ids": self.case_ids,
                "repeat": self.repeat,
                "device_pool": self.device_pool,
                "profiler_groups": self.profiler_groups,
                "seed": self.seed,
            },
        )


class EvidenceKind(str, Enum):
    HOST_PROOF = "host_proof"
    COMPILE = "compile"
    CORRECTNESS = "correctness"
    EVENT_AB = "event_ab"
    PMU = "pmu"
    FORMAL = "formal"
    OFFICIAL_STANDARD = "official_standard"
    OFFICIAL_HIDDEN = "official_hidden"
    SCORE_SNAPSHOT = "score_snapshot"
    LEGACY_DERIVED = "legacy_derived"


class EvidenceStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EvidenceRecord:
    kind: EvidenceKind
    status: EvidenceStatus
    candidate_id: str | None
    experiment_id: str | None
    operator: OperatorId
    source_tree_sha256: str | None
    casepack_sha256: str | None
    payload: Mapping[str, Any]
    artifact_refs: tuple[str, ...]
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", OperatorId.parse(self.operator))
        if self.candidate_id is not None and not self.candidate_id.startswith("candidate_"):
            raise ContractError("candidate_id must be content addressed")
        if self.experiment_id is not None and not self.experiment_id.startswith("experiment_"):
            raise ContractError("experiment_id must be content addressed")
        if self.source_tree_sha256 is not None:
            object.__setattr__(self, "source_tree_sha256", require_sha256(self.source_tree_sha256, "source_tree_sha256"))
        if self.casepack_sha256 is not None:
            object.__setattr__(self, "casepack_sha256", require_sha256(self.casepack_sha256, "casepack_sha256"))
        if not self.artifact_refs:
            raise ContractError("every evidence record must retain immutable artifact references")

    @property
    def evidence_id(self) -> str:
        return content_id("evidence", self)


@dataclass(frozen=True)
class CarrierBundle:
    carrier_operator: OperatorId
    environment_id: str
    component_sources: Mapping[str, str]
    submission_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "carrier_operator", OperatorId.parse(self.carrier_operator))
        if not self.component_sources:
            raise ContractError("carrier bundle cannot be empty")
        normalized = {
            OperatorId.parse(operator).value: require_sha256(digest, f"component_sources[{operator}]")
            for operator, digest in self.component_sources.items()
        }
        object.__setattr__(self, "component_sources", normalized)
        if not self.environment_id.startswith("env_"):
            raise ContractError("carrier bundle requires environment_id")

    @property
    def carrier_id(self) -> str:
        return content_id(
            "carrier",
            {
                "carrier_operator": self.carrier_operator.value,
                "environment_id": self.environment_id,
                "component_sources": self.component_sources,
                "submission_id": self.submission_id,
            },
        )


@dataclass(frozen=True)
class ScoreSnapshot:
    task_version: str
    hardware: str
    total: float
    components: Mapping[str, float]
    observed_at: str
    source: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.total):
            raise ContractError("score total must be finite")
        normalized: dict[str, float] = {}
        for key, value in self.components.items():
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ContractError(f"non-finite component score: {key}")
            normalized[str(key)] = numeric
        object.__setattr__(self, "components", normalized)
        if not all((self.task_version, self.hardware, self.observed_at, self.source)):
            raise ContractError("score snapshot provenance is incomplete")

    @property
    def snapshot_id(self) -> str:
        return content_id("score", self)


def as_payload(value: Any) -> dict[str, Any]:
    plain = _plain(value)
    if not isinstance(plain, dict):
        raise ContractError("expected an object payload")
    return plain


def stable_sequence(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))
