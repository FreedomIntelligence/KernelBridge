import type {
  AcceptanceRequest, ChangeUnit, KnowledgeCandidate, KnowledgeSelection, PrivateRecord, RsiCandidates,
  RsiInvalidation, RsiKnowledgeMemory, RsiMeasuredNode, RsiScopedKnowledge, RsiStatus,
} from "../generated/rsi.ts";
import { canonicalKey, compareText } from "../run/scoring/canonical.ts";
import { artifactDigest } from "./artifacts.ts";
import { readMeasurement } from "./necessity.ts";
import { readRsi, Records, sourceFiles, text } from "./records.ts";
import { readUnitRecords } from "./tracking.ts";

export function invalidatedMeasurements(records: Records): ReadonlySet<string> {
  return new Set(records.all("invalidation").map((record) =>
    readRsi<RsiInvalidation>(record.value, "RsiInvalidation").measurement_id));
}

function currentNode(records: Records, sourceDigest: string): RsiMeasuredNode | null {
  let selected: RsiMeasuredNode | null = null;
  let sequence = -1;
  for (const record of records.all("node")) {
    const node = readRsi<RsiMeasuredNode>(record.value, "RsiMeasuredNode");
    const acceptance = records.get(node.acceptance_id);
    if (!acceptance || acceptance.kind !== "acceptance") throw new Error("RSI node lost its original acceptance");
    if (node.accepted && node.source_digest === sourceDigest && acceptance.sequence > sequence) {
      selected = node;
      sequence = acceptance.sequence;
    }
  }
  return selected;
}

function originalDependencies(records: Records, acceptance: PrivateRecord): readonly KnowledgeCandidate[] | null {
  const turn = text(acceptance.value.turn_id, "memory origin turn");
  const selected = records.get(`${turn}/selection`);
  const pool = records.get(`${turn}/candidates`);
  if (!selected || !pool) return null;
  const selection = readRsi<KnowledgeSelection>(selected.value.selection, "KnowledgeSelection");
  const candidates = readRsi<RsiCandidates>(pool.value.candidates, "RsiCandidates");
  const byId = new Map(candidates.candidates.map((candidate) => [candidate.ku_id, candidate]));
  return selection.selected.map((item) => {
    const candidate = byId.get(item.ku_id);
    if (!candidate) throw new Error("RSI memory exposure has no original candidate");
    return candidate;
  });
}

function newRemovals(acceptance: PrivateRecord): readonly ChangeUnit[] {
  return readUnitRecords(acceptance.value.units)
    .filter((item) => item.state === "deleted" && item.experiment_id === acceptance.value.experiment_id)
    .map((item) => item.unit);
}

function localItem(
  records: Records, acceptance: PrivateRecord, status: RsiStatus, node: RsiMeasuredNode | null,
): RsiScopedKnowledge {
  const measurement = readMeasurement(acceptance.value.confirmation);
  const dependencies = originalDependencies(records, acceptance);
  const generation = dependencies === null || dependencies.some((item) => item.generation === null)
    ? null : 1 + Math.max(0, ...dependencies.map((item) => item.generation!));
  const scope = node === null ? "workspace" : "node";
  const identity = `${acceptance.record_id}/knowledge/${scope}`;
  const content = node === null ? {
    kind: "recorded_attempt", acceptance_id: acceptance.record_id,
    adopted: readRsi<AcceptanceRequest>(acceptance.value.request, "AcceptanceRequest").accept,
    baseline_digest: measurement.keep_digest, candidate_digest: measurement.remove_digest,
    removed_changes: newRemovals(acceptance).map((unit) => ({ path: unit.path, anchor: unit.anchor })),
    interpretation: "This records this attempt only; it does not establish a general prohibition or cross-task benefit.",
  } : {
    kind: "artifact_state", node_id: node.node_id,
    retained_changes: readUnitRecords(acceptance.value.units).filter((item) => item.state !== "deleted")
      .map((item) => ({ unit_id: item.unit.unit_id, path: item.unit.path, anchor: item.unit.anchor })),
  };
  return readRsi<RsiScopedKnowledge>({ scope, node_id: node?.node_id ?? null,
    source_digest: node?.source_digest ?? measurement.remove_digest, measurement_id: measurement.measurement_id,
    environment_id: measurement.environment_id, protocol_id: measurement.protocol_id,
    candidate: { ku_id: identity, text: canonicalKey({ scope, environment_id: measurement.environment_id,
      protocol_id: measurement.protocol_id, source_digest: node?.source_digest ?? measurement.remove_digest, ...content }),
      source_ids: [...new Set(dependencies?.flatMap((item) => item.source_ids) ?? [])].sort(compareText),
      source_kind: "generated", generation,
      status: generation === null ? "review" : "candidate", mandatory: false, rank: acceptance.sequence,
      exposures: status.exposures[identity] ?? 0, new_external: false, origin_task: status.task_id,
      dependencies: dependencies?.map((item) => item.ku_id) ?? [] },
  }, "RsiScopedKnowledge");
}

/** Invalidation follows original dependencies, including facts no longer in the bounded context view. */
function reviewedIdentities(
  records: Records, items: readonly RsiScopedKnowledge[], current: readonly KnowledgeCandidate[], taskId: string,
): ReadonlySet<string> {
  const known = new Map<string, KnowledgeCandidate>();
  const measurements = new Map<string, string>();
  for (const record of records.all("candidates")) {
    for (const candidate of readRsi<RsiCandidates>(record.value.candidates, "RsiCandidates").candidates) {
      known.set(candidate.ku_id, candidate);
    }
    if (record.value.memory !== undefined) {
      const memory = readRsi<RsiKnowledgeMemory>(record.value.memory, "RsiKnowledgeMemory");
      for (const item of memory.items) measurements.set(item.candidate.ku_id, item.measurement_id);
    }
  }
  for (const item of items) measurements.set(item.candidate.ku_id, item.measurement_id);
  for (const candidate of [...current, ...items.map((item) => item.candidate)]) known.set(candidate.ku_id, candidate);
  const invalid = invalidatedMeasurements(records);
  const currentIds = new Set(current.map((candidate) => candidate.ku_id));
  const children = new Map<string, string[]>();
  const review = new Set<string>();
  for (const candidate of known.values()) {
    if (["review", "demoted", "tombstone", "unknown"].includes(candidate.status)
        || invalid.has(measurements.get(candidate.ku_id) ?? "")
        || candidate.origin_task !== taskId
          && !currentIds.has(candidate.ku_id)) review.add(candidate.ku_id);
    for (const dependency of candidate.dependencies) {
      const group = children.get(dependency) ?? [];
      group.push(candidate.ku_id);
      children.set(dependency, group);
      if (!known.has(dependency)) review.add(candidate.ku_id);
    }
  }
  const pending = [...review];
  for (let index = 0; index < pending.length; index += 1) {
    for (const child of children.get(pending[index]) ?? []) {
      if (!review.has(child)) {
        review.add(child);
        pending.push(child);
      }
    }
  }
  return review;
}

/** O(B + D) over bounded record bytes/dependencies; latest 16 world facts plus one exact-artifact note. */
export function knowledgeMemory(
  records: Records, status: RsiStatus, current: readonly KnowledgeCandidate[],
): RsiKnowledgeMemory {
  const sourceDigest = artifactDigest(status.files);
  if (status.config.local_memory === false) {
    return { schema_version: "kernelcat.rsi-memory/v1", source_digest: sourceDigest,
      node_id: null, items: [], omitted: 0 };
  }
  const node = currentNode(records, sourceDigest);
  const retained = records.all("acceptance").filter((record) => record.value.turn_id !== undefined);
  const world = retained.filter((record) => !readRsi<AcceptanceRequest>(record.value.request, "AcceptanceRequest").accept
    || newRemovals(record).length > 0);
  const items = world.slice(-16).map((record) => localItem(records, record, status, null));
  if (node) {
    const acceptance = records.get(node.acceptance_id);
    if (!acceptance || artifactDigest(sourceFiles(acceptance.value.source)) !== sourceDigest) {
      throw new Error("RSI node note has no exact original artifact");
    }
    items.push(localItem(records, acceptance, status, node));
  }
  const review = reviewedIdentities(records, items, current, status.task_id);
  const selected = [...items].reverse().slice(0, Math.max(0, 100 - current.length));
  return readRsi<RsiKnowledgeMemory>({ schema_version: "kernelcat.rsi-memory/v1", source_digest: sourceDigest,
    node_id: node?.node_id ?? null, omitted: world.length + (node === null ? 0 : 1) - selected.length,
    items: selected.map((item) => review.has(item.candidate.ku_id)
      ? { ...item, candidate: { ...item.candidate, status: "review" } } : item),
  }, "RsiKnowledgeMemory");
}
