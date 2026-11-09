import type { EffectRpc } from "../clients/rpc.ts";
import type {
  ChangeUnit, PrivateRecord, ReductionPlan, RsiMeasuredNode, RsiRoundBoundary,
  RsiRoundDecision, RsiRoundObservation, RsiRoundPage,
} from "../generated/rsi.ts";
import { artifactDigest } from "../rsi/artifacts.ts";
import { invalidatedMeasurements } from "../rsi/memory.ts";
import { measurementDecision, readMeasurement } from "../rsi/necessity.ts";
import { readRsi, Records, text } from "../rsi/records.ts";
import { canonicalKey, compareText } from "../run/scoring/canonical.ts";

interface Attempt {
  readonly step: number;
  readonly record: PrivateRecord;
  readonly node: RsiMeasuredNode | undefined;
}

function attempts(records: Records, taskId: string): readonly Attempt[] {
  const nodes = new Map(records.all("node").map((record) => {
    const node = readRsi<RsiMeasuredNode>(record.value, "RsiMeasuredNode");
    return [node.acceptance_id, node] as const;
  }));
  const prefix = `${taskId}/rsi/turn/`;
  return records.all("acceptance").flatMap((record) => {
    const turn = record.value.turn_id;
    if (turn === undefined) return []; // Historical unbound records cannot supply round evidence.
    if (typeof turn !== "string" || !turn.startsWith(prefix) || !/^(0|[1-9][0-9]*)$/.test(turn.slice(prefix.length))) {
      throw new Error("RSI round attempt lacks a canonical task turn");
    }
    const step = Number(turn.slice(prefix.length));
    if (!Number.isSafeInteger(step)) throw new Error("RSI round step exceeds the integer bound");
    return [{ step, record, node: nodes.get(record.record_id) }];
  }).sort((left, right) => left.step - right.step || compareText(left.record.record_id, right.record.record_id));
}

function range(entries: readonly Attempt[], boundary: RsiRoundBoundary): readonly Attempt[] {
  let low = 0;
  let high = entries.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (entries[middle].step < boundary.start_step) low = middle + 1;
    else high = middle;
  }
  let end = low;
  while (end < entries.length && entries[end].step < boundary.end_step) end += 1;
  return entries.slice(low, end);
}

function cloneKey(unit: ChangeUnit): string {
  return canonicalKey([unit.path, unit.anchor, unit.before, unit.after]);
}

function progress(attempt: Attempt, plan: ReductionPlan, invalid: ReadonlySet<string>): boolean | null {
  const measurement = readMeasurement(attempt.record.value.confirmation);
  if (!attempt.node || invalid.has(measurement.measurement_id) || !measurement.independent_confirmation
      || measurement.keep_correct !== true || measurement.remove_correct === null
      || !measurement.holdout_coverage?.length) return null;
  if (measurement.remove_correct === false) return false;
  const decision = measurementDecision(measurement, plan.policy);
  if (decision.lower === null || decision.upper === null) return null;
  if (-decision.upper > plan.policy.delta) return attempt.node.accepted;
  return -decision.lower <= plan.policy.delta ? false : null;
}

function observation(boundary: RsiRoundBoundary, entries: readonly Attempt[], records: Records,
  invalid: ReadonlySet<string>): RsiRoundObservation {
  const gains: (boolean | null)[] = [];
  const invalidIds: string[] = [];
  let units = 0;
  let matches = 0;
  for (const attempt of entries) {
    const experiment = text(attempt.record.value.experiment_id, "round experiment");
    const original = records.get(`${experiment}/plan`);
    if (!original || original.kind !== "plan") throw new Error("RSI round lacks its original reduction plan");
    const plan = readRsi<ReductionPlan>(original.value.plan, "ReductionPlan");
    const tombstones = new Set(plan.tombstones.map(cloneKey));
    units += plan.units.length;
    matches += plan.units.filter((unit) => tombstones.has(cloneKey(unit))).length;
    gains.push(progress(attempt, plan, invalid));
    const measurement = readMeasurement(attempt.record.value.confirmation);
    if (invalid.has(measurement.measurement_id)) invalidIds.push(measurement.measurement_id);
  }
  return readRsi<RsiRoundObservation>({ schema_version: "kernelcat.rsi-round/v1", boundary,
    acceptance_ids: entries.map((entry) => entry.record.record_id),
    node_ids: entries.flatMap((entry) => entry.node ? [entry.node.node_id] : []),
    outcome: entries.length ? "measured" : "empty",
    significant_improvement: gains.includes(true) ? true : gains.includes(null) ? null : false,
    proposal_units: entries.length ? units : null, tombstone_matches: entries.length ? matches : null,
    invalidated_measurement_ids: invalidIds }, "RsiRoundObservation");
}

/** Conservative first policy. Unknown timing never diagnoses program stagnation. */
export function decideRounds(rounds: readonly { id: string; observation: RsiRoundObservation }[],
  invalidationIds: readonly string[] = []): RsiRoundDecision {
  const window = rounds.slice(-3);
  let diagnosis: RsiRoundDecision["diagnosis"] = "insufficient_history";
  let action: RsiRoundDecision["action"] = "continue";
  if (window.some((round) => round.observation.significant_improvement === true)) diagnosis = "progress";
  else if (window.length >= 2 && window.slice(-2).every((round) => round.observation.outcome === "empty")) {
    diagnosis = "empty_rounds";
    action = "reset_context";
  } else if (window.length === 3) {
    const units = window.reduce((sum, round) => sum + (round.observation.proposal_units ?? 0), 0);
    const matches = window.reduce((sum, round) => sum + (round.observation.tombstone_matches ?? 0), 0);
    if (window.some((round) => round.observation.significant_improvement === null)) {
      diagnosis = "measurement_unknown";
      action = "inspect_measurement";
    } else if (units > 0 && matches / units >= 0.5) {
      diagnosis = "repeated_proposals";
      action = "reset_context";
    } else {
      diagnosis = "goal_stagnation";
      action = "replay_candidate";
    }
  }
  return readRsi<RsiRoundDecision>({ schema_version: "kernelcat.rsi-intervention/v1",
    policy_version: "goal-tombstone-v1", round_ids: window.map((round) => round.id),
    invalidation_ids: invalidationIds, diagnosis, action, status: "proposed" }, "RsiRoundDecision");
}

function currentObservations(records: Records, invalid: ReadonlySet<string>): {
  id: string; observation: RsiRoundObservation;
}[] {
  const boundary = records.last("branch")?.sequence ?? 0;
  return records.all("round").filter((record) => record.sequence > boundary).slice(-3).map((record) => {
    const observed = readRsi<RsiRoundObservation>(record.value, "RsiRoundObservation");
    const revoked = observed.acceptance_ids.some((identity) => {
      const accepted = records.get(identity);
      if (!accepted) throw new Error("RSI round references a missing attempt");
      return invalid.has(readMeasurement(accepted.value.confirmation).measurement_id);
    });
    return { id: record.record_id, observation: revoked ? { ...observed, significant_improvement: null } : observed };
  });
}

/** O(E log E + R log E + A) local work; paginated original round identities, no effects rerun. */
export async function observeRounds(records: Records, rpc: EffectRpc, taskId: string): Promise<void> {
  const entries = attempts(records, taskId);
  const invalid = invalidatedMeasurements(records);
  let cursor = 0;
  for (let pageIndex = 0; pageIndex < 1024; pageIndex += 1) {
    const page = readRsi<RsiRoundPage>(await rpc.call("rsi_rounds", { cursor, limit: 64 }), "RsiRoundPage");
    for (const boundary of page.rounds) {
      const identity = `${boundary.request_id}/observation`;
      const existing = records.get(identity);
      if (existing) {
        if (existing.kind !== "round" || canonicalKey(existing.value.boundary) !== canonicalKey(boundary)) {
          throw new Error("RSI original round boundary changed");
        }
      } else await records.write(identity, "round", observation(boundary, range(entries, boundary), records, invalid));
    }
    if (page.next_cursor === null) {
      const observed = currentObservations(records, invalid);
      if (observed.length === 0) return;
      const selected = new Set(observed.flatMap((round) => round.observation.acceptance_ids)
        .map((identity) => readMeasurement(records.get(identity)!.value.confirmation).measurement_id));
      const revocations = records.all("invalidation").filter((record) =>
        typeof record.value.measurement_id === "string" && selected.has(record.value.measurement_id));
      const decision = decideRounds(observed, revocations.map((record) => record.record_id));
      await records.write(`${observed.at(-1)!.id}/decision/${artifactDigest(decision)}`, "intervention", decision);
      return;
    }
    if (page.next_cursor !== cursor + page.rounds.length || page.next_cursor <= cursor) {
      throw new Error("RSI round cursor did not advance in original order");
    }
    cursor = page.next_cursor;
  }
  throw new Error("RSI round replay exceeds 65536 retained rounds");
}
