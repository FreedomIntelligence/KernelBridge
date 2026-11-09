import type {
  AcceptanceRequest, ChangeUnit, Features, Footprint, JsonObject, NecessityDecision,
  PairedMeasurement, ReductionPlan, ReductionRequest,
} from "../generated/rsi.ts";
import { canonicalKey, compareText } from "../run/scoring/canonical.ts";
import { jsonObject, readRsi, text } from "./records.ts";

export interface UnitRecord {
  readonly unit: ChangeUnit;
  readonly state: "proven" | "uncertain" | "review" | "deleted";
  readonly experiment_id: string;
  readonly hypothesis: string;
  readonly decision: NecessityDecision;
}

export interface UnitMeasurement {
  readonly request: ReductionRequest;
  readonly measurement: PairedMeasurement;
}

export interface KnowledgeLink {
  readonly ku_id: string;
  readonly unit_id: string;
  readonly measurement_id: string;
  readonly I: boolean | null;
  readonly V: boolean | null;
  readonly X: null;
}

export function readUnitRecords(value: unknown): readonly UnitRecord[] {
  if (!Array.isArray(value) || value.length > 256) throw new Error("RSI necessity ledger exceeds its unit bound");
  return value.map((raw) => {
    const row = jsonObject(raw);
    if (!["proven", "uncertain", "review", "deleted"].includes(String(row.state))) {
      throw new Error("Invalid RSI necessity state");
    }
    if (typeof row.hypothesis !== "string" || row.hypothesis.length > 8192) {
      throw new Error("Invalid RSI necessity hypothesis");
    }
    return { unit: readRsi<ChangeUnit>(row.unit, "ChangeUnit"), state: row.state as UnitRecord["state"],
      experiment_id: text(row.experiment_id, "unit experiment"), hypothesis: row.hypothesis,
      decision: readRsi<NecessityDecision>(row.decision, "NecessityDecision") };
  });
}

export function footprintMatches(features: Features, rule: Footprint): boolean | null {
  const required = (["runtime", "lowered"] as const).filter((layer) => rule[layer].length > 0);
  if (required.length === 0 || required.some((layer) => features[layer] === null)) return null;
  return required.every((layer) => {
    const observed = new Set(features[layer]!);
    return rule[layer].every((value) => observed.has(value));
  });
}

export function knowledgeLinks(
  plan: ReductionPlan, samples: readonly UnitMeasurement[], decisions: readonly NecessityDecision[],
  rules: readonly Footprint[],
): readonly KnowledgeLink[] {
  const rulesById = new Map(rules.map((rule) => [rule.ku_id, rule]));
  const units = new Map(plan.units.map((unit) => [unit.unit_id, unit]));
  const byMeasurement = new Map(decisions.map((decision) => [decision.measurement_id, decision]));
  return samples.flatMap(({ request, measurement }) => {
    const unit = units.get(request.unit_id)!;
    const decision = byMeasurement.get(measurement.measurement_id)!;
    return unit.ku_ids.map((identity) => {
      const rule = rulesById.get(identity);
      const implemented = rule === undefined ? null : footprintMatches(measurement.features, rule);
      const removed = rule === undefined ? null : footprintMatches(measurement.remove_features, rule);
      const necessity = decision.verdict === "uncertain" ? null : decision.verdict === "necessary";
      return { ku_id: identity, unit_id: unit.unit_id, measurement_id: measurement.measurement_id,
        I: implemented, V: implemented === true && removed === false ? necessity : null,
        X: null };
    });
  });
}

export function updatedUnits(
  prior: readonly UnitRecord[], plan: ReductionPlan, request: AcceptanceRequest,
  measurements: readonly UnitMeasurement[], confirmation?: PairedMeasurement,
): readonly UnitRecord[] {
  const revalidated = plan.integration_due && plan.baseline_digest === plan.candidate_digest
    && request.removed_unit_ids.length === 0 && confirmation?.independent_confirmation === true
    && confirmation.keep_correct === true && confirmation.remove_correct === true
    && confirmation.holdout_coverage !== null && confirmation.holdout_coverage.length > 0;
  if (!request.accept && !revalidated) return prior;
  const changed = new Set(request.accept ? plan.units.flatMap((unit) => [unit.unit_id, unit.path]) : []);
  const updated = new Map(prior.map((item) => [item.unit.unit_id, {
    ...item, state: item.state !== "deleted" && item.unit.dependencies.some((identity) => changed.has(identity))
      ? "review" as const : item.state,
  }]));
  const decisions = new Map(request.decisions.map((decision) => [decision.measurement_id, decision]));
  const byUnit = new Map(measurements.map((item) => [item.request.unit_id, decisions.get(item.measurement.measurement_id)!]));
  for (const unit of plan.units) {
    const decision = byUnit.get(unit.unit_id)!;
    const state = request.removed_unit_ids.includes(unit.unit_id) ? "deleted" :
      decision.verdict === "necessary" ? "proven" : "uncertain";
    updated.set(unit.unit_id, { unit, state, decision, experiment_id: plan.experiment_id,
      hypothesis: plan.proposal.hypothesis });
  }
  if (updated.size > 256) throw new Error("RSI necessity ledger exceeds 256 tracked units");
  return [...updated.values()].sort((left, right) => compareText(left.unit.unit_id, right.unit.unit_id));
}

export function retainedTombstones(
  previous: readonly ChangeUnit[], units: readonly UnitRecord[],
): readonly ChangeUnit[] {
  const unique = new Map(previous.map((unit) => [canonicalKey(unit), unit]));
  for (const record of units) {
    if (record.state === "deleted") unique.set(canonicalKey(record.unit), record.unit);
  }
  if (unique.size > 256) throw new Error("RSI tombstone projection exceeds 256 entries; archive selection is required");
  return [...unique.entries()].sort(([left], [right]) => compareText(left, right)).map(([, unit]) => unit);
}

export function readTombstones(value: unknown): readonly ChangeUnit[] {
  if (!Array.isArray(value) || value.length > 256) throw new Error("Invalid RSI tombstone projection");
  return value.map((unit) => readRsi<ChangeUnit>(unit, "ChangeUnit"));
}

export function coverageDifference(measurement: PairedMeasurement): JsonObject {
  const visible = new Set(measurement.visible_coverage);
  const holdout = measurement.holdout_coverage === null ? null : new Set(measurement.holdout_coverage);
  return { visible: [...visible].sort(compareText), holdout: holdout === null ? null : [...holdout].sort(compareText),
    visible_only: holdout === null ? null : [...visible].filter((id) => !holdout.has(id)).sort(compareText) };
}
