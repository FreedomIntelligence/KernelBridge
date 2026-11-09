import type { ScoringObservation, ScoringResultSet, TaskContract, TaskContractSource } from "../../generated/task-contract.ts";
import { loadTaskContract, validateScoringResultSet } from "../../tasks/contract.ts";
import { canonicalValue } from "./canonical.ts";
import { validateScoringReferences } from "./contract.ts";
import { bindingReasons, domainReasons, officialReasons, orderReasons } from "./eligibility.ts";
import { geometricSpeedup, weightedMean } from "./numeric.ts";
import { uniqueObservations } from "./observations.ts";
import type { UniqueObservation } from "./observations.ts";
import type { EvaluationScore, ScoreReport } from "./types.ts";

export type { EvaluationScore, ExclusionCode, ExclusionReason, ScoreOutcome, ScoreReport } from "./types.ts";

function scoreValue(contract: TaskContract, caseIds: ReadonlyArray<string>, observation: ScoringObservation): number {
  if (contract.scoring.kind === "official") {
    if (typeof observation.official_score !== "number") {
      throw new Error("Scoring invariant failed: eligible observation lacks official score");
    }
    return observation.official_score;
  }
  const weights = caseIds.map((id) => contract.input_domain.cases[id].weight);
  const pairs = caseIds.map((id) => {
    const measurement = observation.measurements[id];
    if (measurement.status !== "measured") {
      throw new Error(`Scoring invariant failed: eligible observation lacks measured pair for ${id}`);
    }
    return measurement;
  });
  const candidates = pairs.map((pair) => pair.candidate_ms);
  return contract.scoring.kind === "weighted_mean_latency" ? weightedMean(weights, candidates)
    : geometricSpeedup(weights, pairs.map((pair) => pair.baseline_ms), candidates);
}

function evaluate(
  contract: TaskContract, digest: string, caseIds: ReadonlyArray<string>, unique: UniqueObservation,
): EvaluationScore {
  const observation = unique.observation;
  const reasons = [...unique.ambiguity, ...bindingReasons(contract, digest, observation),
    ...domainReasons(contract, caseIds, observation), ...officialReasons(contract, observation)];
  if (reasons.length > 0) {
    return { observation, outcome: { status: "ineligible", reasons: orderReasons(reasons) } };
  }
  const value = scoreValue(contract, caseIds, observation);
  if (!Number.isFinite(value) || (contract.scoring.kind !== "official" && value <= 0)) {
    return { observation, outcome: { status: "ineligible", reasons: [{
      code: "unrepresentable_score", path: "score", detail: "final weighted score is outside positive finite double range",
    }] } };
  }
  return { observation, outcome: { status: "scored", value } };
}

export function score(taskContractSource: Uint8Array, resultSet: unknown): ScoreReport {
  if (!(taskContractSource instanceof Uint8Array)) {
    throw new Error("Cannot score TaskContract source: expected original Uint8Array bytes");
  }
  let source: TaskContractSource;
  try {
    source = loadTaskContract(taskContractSource);
  } catch (error) {
    throw new Error(`Cannot score TaskContract source: ${String(error)}`, { cause: error });
  }
  const caseIds = validateScoringReferences(source.contract);
  let projection: ScoringResultSet;
  try {
    projection = validateScoringResultSet(resultSet);
  } catch (error) {
    throw new Error(`Cannot score result projection: ${String(error)}`, { cause: error });
  }
  return {
    task_contract_digest: source.source_digest,
    contract_id: source.contract.contract_id,
    contract_revision: source.contract.revision,
    scoring: canonicalValue(source.contract.scoring),
    source_kind: projection.source_kind,
    results: uniqueObservations(projection.observations)
      .map((observation) => evaluate(source.contract, source.source_digest, caseIds, observation)),
  };
}
