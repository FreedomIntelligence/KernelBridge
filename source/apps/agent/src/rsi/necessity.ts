import { wireValidator } from "../clients/wire-schemas.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { SCHEMA } from "../generated/rsi.ts";
import type { NecessityDecision, PairedMeasurement, RsiPolicy } from "../generated/rsi.ts";

const measurementValidator = wireValidator<PairedMeasurement>(SCHEMA.$id, "PairedMeasurement");
const policyValidator = wireValidator<RsiPolicy>(SCHEMA.$id, "RsiPolicy");
const decisionValidator = wireValidator<NecessityDecision>(SCHEMA.$id, "NecessityDecision");

// One-sided .95 Student t quantiles: paired 90% CI / TOST alpha=.05.
// Values are rounded upward; df > 30 conservatively retains the df=30 value.
const T95 = [
  6.313752, 2.919986, 2.353364, 2.131847, 2.015049, 1.943181,
  1.894579, 1.859549, 1.833113, 1.812462, 1.795885, 1.782288,
  1.770934, 1.761311, 1.753051, 1.745884, 1.739607, 1.734064,
  1.729133, 1.724719, 1.720743, 1.717145, 1.713872, 1.710883,
  1.708141, 1.705618, 1.703289, 1.701131, 1.699128, 1.697261,
] as const;

export function readPolicy(value: unknown): RsiPolicy {
  const policy = validatedSnapshot(value, policyValidator, "RSI policy");
  if (policy.delta <= policy.epsilon) {
    throw new Error("RSI policy delta must exceed epsilon");
  }
  return policy;
}

export function readMeasurement(value: unknown): PairedMeasurement {
  const measurement = validatedSnapshot(value, measurementValidator, "RSI measurement");
  const pairs = measurement.sample_ids.length;
  if (measurement.keep_log_scores.length !== pairs || measurement.remove_log_scores.length !== pairs) {
    throw new Error(`RSI measurement ${measurement.measurement_id} has unpaired sample lengths`);
  }
  for (const [label, identities] of [
    ["sample", measurement.sample_ids], ["evidence", measurement.evidence_ids],
  ] as const) {
    if (new Set(identities).size !== identities.length) {
      throw new Error(`RSI measurement ${measurement.measurement_id} has duplicate ${label} identities`);
    }
  }
  return measurement;
}

function decision(
  measurement: PairedMeasurement, verdict: NecessityDecision["verdict"], reason: string,
  interval: { mean: number; lower: number; upper: number } | null = null,
): NecessityDecision {
  const result: NecessityDecision = {
    verdict, reason, mean: interval?.mean ?? null, lower: interval?.lower ?? null,
    upper: interval?.upper ?? null, pairs: measurement.sample_ids.length,
    measurement_id: measurement.measurement_id,
  };
  if (!decisionValidator(result)) {
    throw new Error(`RSI measurement ${measurement.measurement_id} produces an interval outside the decision contract`);
  }
  return result;
}

/** Fixed-sample interval for finite paired differences already checked at the evidence boundary. */
export function pairedInterval(differences: readonly number[]): { mean: number; lower: number; upper: number } | null {
  let mean = 0;
  let squaredDeviation = 0;
  const count = differences.length;
  if (count < 2) {
    return null;
  }
  for (let index = 0; index < count; index += 1) {
    const difference = differences[index];
    const offset = difference - mean;
    mean += offset / (index + 1);
    squaredDeviation += offset * (difference - mean);
  }
  if (squaredDeviation <= 0) {
    return null;
  }
  const standardError = Math.sqrt(squaredDeviation / (count - 1) / count);
  const margin = T95[Math.min(count - 1, 30) - 1] * standardError;
  return { mean, lower: mean - margin, upper: mean + margin };
}

function evidenceDecision(measurement: PairedMeasurement, policy: RsiPolicy): NecessityDecision | null {
  if (measurement.keep_correct !== true) {
    return decision(measurement, "uncertain", "keep_correctness_not_established");
  }
  if (measurement.evidence_ids.length === 0 || measurement.sample_ids.length === 0) {
    return decision(measurement, "uncertain", "independent_evidence_missing");
  }
  if (measurement.remove_correct === false) {
    return decision(measurement, "necessary", "necessary_for_correctness");
  }
  if (measurement.remove_correct === null) {
    return decision(measurement, "uncertain", "remove_correctness_unknown");
  }
  if (measurement.sample_ids.length < policy.minimum_pairs) {
    return decision(measurement, "uncertain", "insufficient_pairs");
  }
  return null;
}

/** Fixed-sample interpretation only: callers must not repeatedly peek or stop on significance. */
export function measurementDecision(measurement: PairedMeasurement, policy: RsiPolicy): NecessityDecision {
  const evidence = evidenceDecision(measurement, policy);
  if (evidence !== null) {
    return evidence;
  }
  const interval = pairedInterval(measurement.keep_log_scores.map((score, index) =>
    score - measurement.remove_log_scores[index]));
  if (interval === null) {
    return decision(measurement, "uncertain", "zero_observed_variance");
  }
  if (interval.lower > policy.delta) {
    return decision(measurement, "necessary", "paired_improvement_above_delta", interval);
  }
  if (interval.lower > -policy.epsilon && interval.upper < policy.epsilon) {
    return decision(measurement, "redundant", "paired_equivalence_within_epsilon", interval);
  }
  return decision(measurement, "uncertain", "paired_interval_inconclusive", interval);
}

/** O(n) time / O(n) detached validation space, bounded by the measurement schema. */
export function judgeNecessity(value: unknown, policy: RsiPolicy): NecessityDecision {
  return measurementDecision(readMeasurement(value), readPolicy(policy));
}
