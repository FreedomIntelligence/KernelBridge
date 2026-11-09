import { wireValidator } from "../clients/wire-schemas.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { SCHEMA } from "../generated/rsi.ts";
import type {
  AcceptanceRequest, Complexity, ConfirmationRequest, NecessityDecision,
  PairedMeasurement, ReductionPlan, ReductionRequest,
} from "../generated/rsi.ts";
import { compareText } from "../run/scoring/canonical.ts";
import { measurementDecision, readMeasurement } from "./necessity.ts";

const planValidator = wireValidator<ReductionPlan>(SCHEMA.$id, "ReductionPlan");

interface FiniteRpc {
  call(method: string, params: unknown): Promise<unknown>;
}

interface RetainedCandidate {
  digest: string;
  complexity: Complexity;
  removed: string[];
  decisions: NecessityDecision[];
  sampleIds: Set<string>;
  measurementIds: Set<string>;
  provenance: { environment: string; protocol: string } | null;
}

function readPlan(value: unknown): ReductionPlan {
  const plan = validatedSnapshot(value, planValidator, "RSI reduction plan");
  if (plan.policy.delta <= plan.policy.epsilon) {
    throw new Error("RSI policy delta must exceed epsilon");
  }
  if (plan.units.length > plan.policy.max_units) {
    throw new Error("RSI reduction exceeds the policy unit bound");
  }
  if (new Set(plan.units.map((unit) => unit.unit_id)).size !== plan.units.length) {
    throw new Error(`RSI experiment ${plan.experiment_id} has duplicate unit identities`);
  }
  return plan;
}

function sameComplexity(left: Complexity, right: Complexity): boolean {
  return left.nodes === right.nodes && left.branches === right.branches
    && left.specializations === right.specializations && left.cognitive === right.cognitive
    && left.configuration === right.configuration;
}

function bindMeasurement(
  measurement: PairedMeasurement, plan: ReductionPlan, retained: RetainedCandidate,
  keepDigest: string, keepComplexity: Complexity,
): void {
  if (measurement.task_id !== plan.task_id || measurement.keep_digest !== keepDigest
      || !sameComplexity(measurement.keep_complexity, keepComplexity)) {
    throw new Error(`RSI measurement ${measurement.measurement_id} mismatches the expected task or keep artifact`);
  }
  if (retained.measurementIds.has(measurement.measurement_id)) {
    throw new Error(`RSI measurement identity reused: ${measurement.measurement_id}`);
  }
  const provenance = retained.provenance;
  if (provenance !== null && (measurement.environment_id !== provenance.environment
      || measurement.protocol_id !== provenance.protocol)) {
    throw new Error(`RSI measurement ${measurement.measurement_id} changes environment or protocol`);
  }
  retained.provenance ??= { environment: measurement.environment_id, protocol: measurement.protocol_id };
  retained.measurementIds.add(measurement.measurement_id);
}

async function measureUnit(
  plan: ReductionPlan, unitId: string, retained: RetainedCandidate, rpc: FiniteRpc,
): Promise<void> {
  const request: ReductionRequest = {
    experiment_id: plan.experiment_id, unit_id: unitId, removed_unit_ids: [...retained.removed],
  };
  const measurement = readMeasurement(await rpc.call("rsi_measure", request));
  bindMeasurement(measurement, plan, retained, retained.digest, retained.complexity);
  for (const sample of measurement.sample_ids) {
    retained.sampleIds.add(sample);
  }
  const decision = measurementDecision(measurement, plan.policy);
  retained.decisions.push(decision);
  if (prefersUnitRemoval(measurement, decision)) {
    retained.removed.push(unitId);
    retained.digest = measurement.remove_digest;
    retained.complexity = measurement.remove_complexity;
  }
}

function fixedComplexity(complexity: Complexity): bigint {
  // Equal fixed weights across the schema's five dimensions; BigInt preserves
  // comparisons even when individually safe integer dimensions sum above 2^53.
  return BigInt(complexity.nodes) + BigInt(complexity.branches) + BigInt(complexity.specializations)
    + BigInt(complexity.cognitive) + BigInt(complexity.configuration);
}

export function prefersUnitRemoval(measurement: PairedMeasurement, decision: NecessityDecision): boolean {
  // Reverting a deletion may restore dead code. Equivalence permits rollback
  // only when complexity does not increase; ties prefer the prior implementation.
  return decision.verdict === "redundant"
    && fixedComplexity(measurement.remove_complexity) <= fixedComplexity(measurement.keep_complexity);
}

export function acceptsConfirmation(measurement: PairedMeasurement, plan: ReductionPlan): boolean {
  if (!measurement.independent_confirmation || measurement.keep_correct !== true || measurement.remove_correct !== true
      || measurement.holdout_coverage === null || measurement.holdout_coverage.length === 0) {
    return false;
  }
  const decision = measurementDecision(measurement, plan.policy);
  if (decision.upper === null) {
    return false;
  }
  const baseline = fixedComplexity(measurement.keep_complexity);
  const candidate = fixedComplexity(measurement.remove_complexity);
  const increase = candidate > baseline ? Number(candidate - baseline) : 0;
  const threshold = plan.policy.delta + plan.policy.complexity_penalty * increase;
  // The measurement is baseline minus candidate; negate the upper endpoint
  // to obtain the candidate improvement's lower endpoint.
  return -decision.upper > threshold || (decision.verdict === "redundant" && candidate < baseline);
}

async function confirm(plan: ReductionPlan, retained: RetainedCandidate, rpc: FiniteRpc): Promise<PairedMeasurement> {
  const request: ConfirmationRequest = { experiment_id: plan.experiment_id, removed_unit_ids: [...retained.removed] };
  const measurement = readMeasurement(await rpc.call("rsi_confirm", request));
  bindMeasurement(measurement, plan, retained, plan.baseline_digest, plan.baseline_complexity);
  if (measurement.remove_digest !== retained.digest || !sameComplexity(measurement.remove_complexity, retained.complexity)) {
    throw new Error(`RSI confirmation ${measurement.measurement_id} mismatches the retained artifact`);
  }
  if (measurement.sample_ids.some((sample) => retained.sampleIds.has(sample))) {
    throw new Error(`RSI confirmation ${measurement.measurement_id} reuses a selection sample`);
  }
  return measurement;
}

/**
 * O(k) sequential finite RPCs, followed by one confirmation and one acceptance.
 * Local ordering costs O(k log k); retained evidence IDs require O(k * n) space.
 * k <= policy.max_units <= 64, n <= the measurement schema's sample bound.
 * Rejections and unknown RPC outcomes propagate; this routine never retries.
 */
export async function reduceExperiment(value: unknown, rpc: FiniteRpc): Promise<unknown> {
  const plan = readPlan(value);
  const retained: RetainedCandidate = {
    digest: plan.candidate_digest, complexity: plan.candidate_complexity,
    removed: [], decisions: [], sampleIds: new Set(), measurementIds: new Set(), provenance: null,
  };
  const units = [...plan.units].sort((left, right) => compareText(left.unit_id, right.unit_id));
  for (const unit of units) {
    await measureUnit(plan, unit.unit_id, retained, rpc);
  }
  const measurement = await confirm(plan, retained, rpc);
  const request: AcceptanceRequest = {
    experiment_id: plan.experiment_id, removed_unit_ids: [...retained.removed],
    measurement_id: measurement.measurement_id, decisions: retained.decisions,
    accept: acceptsConfirmation(measurement, plan),
  };
  return rpc.call("rsi_accept", request);
}
