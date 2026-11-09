import type { ScorePolicy, ScoringObservation, ScoringResultSet } from "../../generated/task-contract.ts";

export type ExclusionCode =
  | "ambiguous_receipt_revisions" | "conflicting_evaluation_identity"
  | "contract_digest_mismatch" | "casepack_digest_mismatch" | "baseline_mismatch"
  | "timing_mismatch" | "measurement_protocol_mismatch" | "screening_stage"
  | "unknown_environment" | "unknown_device" | "target_vendor_mismatch" | "target_architecture_mismatch"
  | "missing_correctness" | "failed_correctness" | "unknown_correctness" | "extra_correctness"
  | "missing_measurement" | "unknown_measurement" | "extra_measurement"
  | "undeclared_specialization" | "missing_official_score" | "unknown_official_score"
  | "unrepresentable_score";

export interface ExclusionReason {
  readonly code: ExclusionCode;
  readonly path: string;
  readonly detail: string;
}

export type ScoreOutcome =
  | { readonly status: "scored"; readonly value: number }
  | { readonly status: "ineligible"; readonly reasons: ReadonlyArray<ExclusionReason> };

export interface EvaluationScore {
  readonly observation: ScoringObservation;
  readonly outcome: ScoreOutcome;
}

// Internal arithmetic/domain result, not a new wire, receipt or acceptance protocol.
export interface ScoreReport {
  readonly task_contract_digest: string;
  readonly contract_id: string;
  readonly contract_revision: number;
  readonly scoring: ScorePolicy;
  readonly source_kind: ScoringResultSet["source_kind"];
  readonly results: ReadonlyArray<EvaluationScore>;
}
