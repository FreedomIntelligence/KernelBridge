import type {
  JsonObject, KnowledgeComparisonArm, KnowledgeComparisonInput, KnowledgeComparisonPair,
} from "../generated/rsi.ts";
import { canonicalKey, compareText } from "../run/scoring/canonical.ts";
import { pairedInterval, readMeasurement } from "./necessity.ts";
import { readRsi } from "./records.ts";

interface PairObservation {
  readonly pair: KnowledgeComparisonPair;
  readonly difference: number | null;
  readonly reason: string | null;
}

function mean(values: readonly number[]): number {
  return values.reduce((average, value, index) => average + (value - average) / (index + 1), 0);
}

function uniquePairs(input: KnowledgeComparisonInput): readonly KnowledgeComparisonPair[] {
  const pairs = new Map<string, KnowledgeComparisonPair>();
  for (const pair of input.pairs) {
    const previous = pairs.get(pair.pair_id);
    if (previous && canonicalKey(previous) !== canonicalKey(pair)) {
      throw new Error(`Knowledge comparison pair changed: ${pair.pair_id}`);
    }
    pairs.set(pair.pair_id, pair);
  }
  return [...pairs.values()].sort((left, right) => compareText(left.pair_id, right.pair_id));
}

function reserveIdentity(identities: Set<string>, identity: string, label: string): void {
  if (identities.has(identity)) {
    throw new Error(`Knowledge comparison reuses ${label}: ${identity}`);
  }
  identities.add(identity);
}

function checkTask(pair: KnowledgeComparisonPair, taskSplits: Map<string, string>): void {
  if (pair.on.task_key !== pair.off.task_key || pair.on.baseline_digest !== pair.off.baseline_digest) {
    throw new Error(`Knowledge comparison ${pair.pair_id} changes task or initial source between arms`);
  }
  const split = taskSplits.get(pair.on.task_key);
  if (split && split !== pair.split) {
    throw new Error(`Knowledge comparison task appears in train and holdout: ${pair.on.task_key}`);
  }
  if (split) {
    throw new Error(`Knowledge comparison reuses a task as another independent pair: ${pair.on.task_key}`);
  }
  taskSplits.set(pair.on.task_key, pair.split);
}

function checkIdentities(pairs: readonly KnowledgeComparisonPair[]): void {
  const runs = new Set<string>();
  const measurements = new Set<string>();
  const samples = new Set<string>();
  const taskSplits = new Map<string, string>();
  let conditions: string | null = null;
  for (const pair of pairs) {
    checkTask(pair, taskSplits);
    for (const arm of [pair.on, pair.off]) {
      const measurement = readMeasurement(arm.measurement);
      const binding = canonicalKey([arm.model_id, measurement.environment_id, measurement.protocol_id]);
      if (conditions !== null && conditions !== binding) {
        throw new Error("Knowledge comparison changes model, environment or protocol");
      }
      conditions = binding;
      if (measurement.keep_digest !== arm.baseline_digest || measurement[`${arm.score_side}_digest`] !== arm.source_digest) {
        throw new Error(`Knowledge comparison source differs from its measured artifact: ${arm.run_id}`);
      }
      reserveIdentity(runs, arm.run_id, "run identity");
      reserveIdentity(measurements, measurement.measurement_id, "measurement identity");
      for (const sample of measurement.sample_ids) {
        reserveIdentity(samples, sample, "sample identity");
      }
    }
  }
}

function unqualified(arm: KnowledgeComparisonArm, enabled: boolean): string | null {
  if (arm.exposures === null || arm.exposure_unknown === null || arm.exposure_unknown > 0) {
    return "exposure_unknown";
  }
  if (enabled ? arm.exposures === 0 : arm.exposures !== 0) {
    return enabled ? "on_not_exposed" : "off_exposed";
  }
  const measurement = arm.measurement;
  if (!measurement.independent_confirmation || measurement.holdout_coverage === null
      || measurement.holdout_coverage.length === 0) {
    return "independent_confirmation_missing";
  }
  if (measurement.sample_ids.length === 0 || measurement.evidence_ids.length === 0) {
    return "measurement_evidence_missing";
  }
  const correct = measurement[`${arm.score_side}_correct`];
  if (correct !== true) {
    return correct === false ? "incorrect_artifact" : "correctness_unknown";
  }
  return null;
}

function observePair(pair: KnowledgeComparisonPair): PairObservation {
  for (const arm of ["on", "off"] as const) {
    const reason = unqualified(pair[arm], arm === "on");
    if (reason !== null) {
      return { pair, difference: null, reason: `${arm}:${reason}` };
    }
  }
  const scores = (arm: KnowledgeComparisonArm) => arm.measurement[`${arm.score_side}_log_scores`];
  return { pair, difference: mean(scores(pair.on)) - mean(scores(pair.off)), reason: null };
}

function summary(rows: readonly PairObservation[], input: KnowledgeComparisonInput): JsonObject {
  const differences = rows.flatMap((row) => row.difference === null ? [] : [row.difference]);
  const complete = differences.length === rows.length;
  const enough = differences.length >= input.minimum_pairs;
  const interval = complete && enough ? pairedInterval(differences) : null;
  let verdict = "uncertain";
  if (interval !== null) {
    if (interval.lower > input.epsilon) {
      verdict = "positive";
    } else if (interval.upper < -input.epsilon) {
      verdict = "negative";
    } else if (interval.lower > -input.epsilon && interval.upper < input.epsilon) {
      verdict = "equivalent";
    }
  }
  return { pairs: differences.length, unqualified_pairs: rows.length - differences.length,
    mean: differences.length === 0 ? null : mean(differences), lower: interval?.lower ?? null,
    upper: interval?.upper ?? null, verdict,
    reason: !complete ? "unqualified_pairs_present" : !enough ? "insufficient_independent_pairs"
      : interval === null ? "zero_observed_variance" : "paired_90_percent_interval" };
}

/**
 * Read-only analysis of protected, original-owner observations, not a model tool.
 * One independently generated run pair is one statistical observation, regardless
 * of the number of benchmark samples inside each confirmation. Fixed-sample only;
 * this report grants no publication, causal-attribution or execution authority.
 * O(P * S + P log P) time and O(P * S) space, P <= 256 and S <= 256 per arm.
 */
export function buildKnowledgeComparison(value: unknown): JsonObject {
  const input = readRsi<KnowledgeComparisonInput>(value, "KnowledgeComparisonInput");
  if (Buffer.byteLength(canonicalKey(input)) > 16 * 1024 * 1024) {
    throw new Error("Knowledge comparison exceeds 16 MiB");
  }
  const pairs = uniquePairs(input);
  checkIdentities(pairs);
  const rows = pairs.map(observePair);
  const train = summary(rows.filter((row) => row.pair.split === "train"), input);
  const holdout = summary(rows.filter((row) => row.pair.split === "holdout"), input);
  return readRsi<JsonObject>({ schema_version: "kernelcat.knowledge-comparison-report/v1", comparison_id: input.comparison_id,
    estimand: "paired_observed_log_score_on_minus_off", minimum_pairs: input.minimum_pairs, epsilon: input.epsilon,
    train, holdout, generalization_gap: {
      mean: typeof train.mean === "number" && typeof holdout.mean === "number" ? train.mean - holdout.mean : null,
      uncertainty: null, reason: "descriptive_difference_of_separate_task_groups" },
    pairs: rows.map(({ pair, difference, reason }) => ({ pair_id: pair.pair_id, split: pair.split,
      task_key: pair.on.task_key, on_run: pair.on.run_id, off_run: pair.off.run_id,
      on_measurement: pair.on.measurement.measurement_id, off_measurement: pair.off.measurement.measurement_id,
      difference, reason })),
  }, "JsonObject");
}
