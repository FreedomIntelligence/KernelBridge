import type { ScoringObservation } from "../../generated/task-contract.ts";
import { canonicalKey, canonicalValue, compareText } from "./canonical.ts";
import { normalizeProtocol } from "./protocol.ts";
import type { ExclusionReason } from "./types.ts";

export interface UniqueObservation {
  readonly observation: ScoringObservation;
  readonly key: string;
  readonly ambiguity: ReadonlyArray<ExclusionReason>;
}

function normalizeObservation(observation: ScoringObservation): ScoringObservation {
  return canonicalValue({
    ...observation,
    specialization_ids: [...observation.specialization_ids].sort(compareText),
    measurement_protocol: normalizeProtocol(observation.measurement_protocol),
  });
}

function ambiguityReasons(observations: ReadonlyArray<ScoringObservation>): ReadonlyArray<ExclusionReason> {
  const reasons: ExclusionReason[] = [];
  const revisions = new Set(observations.map((observation) => observation.receipt_revision));
  if (revisions.size > 1) {
    reasons.push({ code: "ambiguous_receipt_revisions", path: "receipt_revision",
      detail: "evaluation has multiple receipt revisions; revision adoption belongs to its owner" });
  }
  const counts = new Map<number, number>();
  for (const observation of observations) {
    counts.set(observation.receipt_revision, (counts.get(observation.receipt_revision) ?? 0) + 1);
  }
  if ([...counts.values()].some((count) => count > 1)) {
    reasons.push({ code: "conflicting_evaluation_identity", path: "evaluation_id",
      detail: "evaluation/revision has conflicting observation contents" });
  }
  return reasons;
}

export function uniqueObservations(observations: ReadonlyArray<ScoringObservation>): ReadonlyArray<UniqueObservation> {
  const evaluations = new Map<string, Map<string, ScoringObservation>>();
  for (const supplied of observations) {
    const observation = normalizeObservation(supplied);
    const variants = evaluations.get(observation.evaluation_id) ?? new Map<string, ScoringObservation>();
    variants.set(canonicalKey(observation), observation);
    evaluations.set(observation.evaluation_id, variants);
  }
  const results: UniqueObservation[] = [];
  for (const variants of evaluations.values()) {
    const ambiguity = ambiguityReasons([...variants.values()]);
    for (const [key, observation] of variants) {
      results.push({ observation, key, ambiguity });
    }
  }
  return results.sort(compareObservations);
}

function compareObservations(left: UniqueObservation, right: UniqueObservation): number {
  const a = left.observation;
  const b = right.observation;
  return compareText(a.evaluation_id, b.evaluation_id)
    || a.receipt_revision - b.receipt_revision
    || compareText(a.receipt_digest, b.receipt_digest)
    || compareText(a.program_id, b.program_id)
    || compareText(a.artifact_digest, b.artifact_digest)
    || compareText(left.key, right.key);
}
