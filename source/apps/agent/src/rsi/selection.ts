import { createHash } from "node:crypto";
import { wireValidator } from "../clients/wire-schemas.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { SCHEMA } from "../generated/rsi.ts";
import type { Exposure, KnowledgeCandidate, KnowledgeSelection, RsiCandidates } from "../generated/rsi.ts";
import { compareText } from "../run/scoring/canonical.ts";

const candidatesValidator = wireValidator<RsiCandidates>(SCHEMA.$id, "RsiCandidates");

function seededDraw(seed: number, turnId: string): (limit: number) => number {
  const namespace = JSON.stringify([seed, turnId]);
  let counter = 0;
  return (limit) => {
    const ceiling = Math.floor(0x100000000 / limit) * limit;
    // Rejection avoids modulo bias. A bounded failure produces no selection.
    for (let attempt = 0; attempt < 64; attempt += 1) {
      const value = createHash("sha256").update(`${namespace}:${counter}`).digest().readUInt32BE(0);
      counter += 1;
      if (value < ceiling) {
        return value % limit;
      }
    }
    throw new Error("RSI selection could not draw an unbiased bounded sample");
  };
}

function optionalCandidate(candidate: KnowledgeCandidate): boolean {
  return !candidate.mandatory && candidate.generation !== null && candidate.generation <= 2
    && (candidate.status === "promoted" || candidate.status === "candidate");
}

function sampleStratum(
  candidates: readonly KnowledgeCandidate[], count: number, probability: number,
  draw: (limit: number) => number,
): Exposure[] {
  const pool = [...candidates];
  const result: Exposure[] = [];
  for (let index = 0; index < count; index += 1) {
    const selected = index + draw(pool.length - index);
    [pool[index], pool[selected]] = [pool[selected], pool[index]];
    result.push({ ku_id: pool[index].ku_id, probability, slot: "exploration" });
  }
  return result;
}

function explore(pool: readonly KnowledgeCandidate[], count: number, draw: (limit: number) => number): Exposure[] {
  if (count === 0) {
    return [];
  }
  const external = pool.filter((item) => item.new_external && item.source_kind === "external");
  const other = pool.filter((item) => !(item.new_external && item.source_kind === "external"));
  const minimum = Math.max(0, count - other.length);
  const maximum = Math.min(count, external.length);
  const clamp = (value: number) => Math.max(minimum, Math.min(maximum, value));
  const lower = clamp(Math.floor(count / 2));
  const upper = clamp(Math.ceil(count / 2));
  const externalCount = lower === upper ? lower : lower + draw(2);
  const expectedExternal = (lower + upper) / 2;
  return [
    ...sampleStratum(external, externalCount, external.length ? expectedExternal / external.length : 0, draw),
    ...sampleStratum(other, count - externalCount, other.length ? (count - expectedExternal) / other.length : 0, draw),
  ].sort((left, right) => compareText(left.ku_id, right.ku_id));
}

/** O(n log n) time and O(n) space; schema bounds n to 100 candidates. */
export function selectKnowledge(value: unknown): KnowledgeSelection {
  const { candidates, policy, turn_id } = validatedSnapshot(value, candidatesValidator, "RSI candidates");
  if (policy.delta <= policy.epsilon) {
    throw new Error("RSI policy delta must exceed epsilon");
  }
  if (new Set(candidates.map((item) => item.ku_id)).size !== candidates.length) {
    throw new Error("RSI candidates contain duplicate knowledge identities");
  }
  const ordered = [...candidates].sort((left, right) => compareText(left.ku_id, right.ku_id));
  const mandatory = ordered.filter((item) => item.mandatory);
  if (mandatory.length > policy.slots) {
    throw new Error("Mandatory RSI knowledge exceeds available slots");
  }
  const free = policy.slots - mandatory.length;
  const eligible = ordered.filter(optionalCandidate);
  const reserved = Math.min(eligible.length, Math.ceil(free * policy.exploration_fraction));
  const ordinary = eligible.filter((item) => item.status === "promoted")
    .sort((left, right) => left.rank - right.rank || compareText(left.ku_id, right.ku_id))
    .slice(0, Math.min(free - reserved, eligible.length - reserved));
  const ordinaryIds = new Set(ordinary.map((item) => item.ku_id));
  const pool = eligible.filter((item) => !ordinaryIds.has(item.ku_id));
  const selected: Exposure[] = [
    ...mandatory.map((item): Exposure => ({ ku_id: item.ku_id, probability: 1, slot: "required" })),
    ...ordinary.map((item): Exposure => ({ ku_id: item.ku_id, probability: 1, slot: "ordinary" })),
    ...explore(pool, Math.min(free - ordinary.length, pool.length), seededDraw(policy.seed, turn_id)),
  ];
  return { turn_id, seed: policy.seed, selected };
}
