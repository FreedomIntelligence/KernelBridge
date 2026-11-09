import type { JsonObject, KnowledgeCandidate, KnowledgeSelection, PrivateRecord, RsiCandidates, RsiKnowledgeMemory, RsiStatus } from "../generated/rsi.ts";
import { canonicalKey, compareText } from "../run/scoring/canonical.ts";
import { jsonObject, readRsi } from "./records.ts";
import { buildKnowledgeComparison } from "./knowledge-comparison.ts";

export type ExposureEvidence = Pick<RsiStatus, "exposures" | "exposure_unknown">;

function diversity(counts: readonly number[]): JsonObject {
  const total = counts.reduce((sum, count) => sum + count, 0);
  if (total === 0) return { sources: 0, entropy: null, gini: null };
  const ordered = [...counts].sort((left, right) => left - right);
  const weighted = ordered.reduce((sum, value, index) => sum + (index + 1) * value, 0);
  return { sources: counts.length,
    entropy: -counts.reduce((sum, count) => sum + count / total * Math.log(count / total), 0),
    gini: 2 * weighted / (counts.length * total) - (counts.length + 1) / counts.length };
}

/** Operator-only projection. O(E log E + S log S); no publication authority. */
export function buildRsiReport(
  records: readonly PrivateRecord[], observations: ExposureEvidence | null = null, knowledgeComparison: unknown = null,
): JsonObject {
  if (records.length > 65536) throw new Error("RSI report exceeds its record bound");
  const unique = new Map<string, PrivateRecord>();
  let bytes = 0;
  for (const raw of records) {
    const record = readRsi<PrivateRecord>(raw, "PrivateRecord");
    const previous = unique.get(record.record_id);
    if (previous && canonicalKey(previous) !== canonicalKey(record)) throw new Error("RSI report received conflicting original records");
    unique.set(record.record_id, record);
    bytes += previous ? 0 : Buffer.byteLength(canonicalKey(record));
    if (bytes > 128 * 1024 * 1024) throw new Error("RSI report exceeds its retained byte bound");
  }
  const ordered = [...unique.values()].sort((left, right) => left.sequence - right.sequence || compareText(left.record_id, right.record_id));
  const known = new Map<string, KnowledgeCandidate>();
  const counts = { queries: 0, retrieved: 0, recalled_local: 0, selected: 0, cited: 0, implemented: 0, necessary: 0,
    implementation_unknown: 0, necessity_unknown: 0, cited_without_implementation: 0, implemented_without_citation: 0 };
  const citations = new Map<string, Set<string>>();
  for (const record of ordered) {
    if (record.kind === "candidates") {
      const candidates = readRsi<RsiCandidates>(record.value.candidates, "RsiCandidates");
      counts.queries += record.value.query === null ? 0 : 1;
      const local = record.value.memory === undefined ? 0
        : readRsi<RsiKnowledgeMemory>(record.value.memory, "RsiKnowledgeMemory").items.length;
      counts.retrieved += candidates.candidates.length - local;
      counts.recalled_local += local;
      for (const candidate of candidates.candidates) known.set(candidate.ku_id, candidate);
    } else if (record.kind === "selection") {
      counts.selected += readRsi<KnowledgeSelection>(record.value.selection, "KnowledgeSelection").selected.length;
    } else if (record.kind === "proposal") {
      const proposal = readRsi<{ readonly cites: readonly string[] }>(record.value.proposal, "ExperimentProposal");
      citations.set(record.record_id, new Set(proposal.cites));
      counts.cited += new Set(proposal.cites).size;
    }
  }
  const acceptance = acceptanceSummary(ordered, citations, counts);
  const sources = new Map<string, number>();
  for (const candidate of known.values()) {
    for (const source of candidate.source_ids) sources.set(source, (sources.get(source) ?? 0) + 1);
  }
  return { schema_version: "kernelcat.rsi-report/v1", counts: { ...counts,
    exposed: observations === null ? null : Object.values(observations.exposures).reduce((sum, value) => sum + value, 0),
    exposure_unknown: observations === null ? null : Object.values(observations.exposure_unknown).reduce((sum, value) => sum + value, 0) },
    knowledge: [...known.values()].sort((left, right) => compareText(left.ku_id, right.ku_id)).map((candidate) => ({
      ku_id: candidate.ku_id, source_kind: candidate.source_kind, generation: candidate.generation,
      source_ids: candidate.source_ids, origin_task: candidate.origin_task,
      exposures: observations === null ? null : observations.exposures[candidate.ku_id] ?? 0,
      exposure_unknown: observations === null ? null : observations.exposure_unknown[candidate.ku_id] ?? 0 })),
    diversity: diversity([...sources.values()]), ...acceptance,
    X: null, knowledge_on_off: knowledgeComparison === null ? null : buildKnowledgeComparison(knowledgeComparison) };
}

function acceptanceSummary(
  records: readonly PrivateRecord[], citations: ReadonlyMap<string, ReadonlySet<string>>,
  counts: { implemented: number; necessary: number; implementation_unknown: number; necessity_unknown: number;
    cited_without_implementation: number; implemented_without_citation: number },
): JsonObject {
  const tasks = new Map<string, Set<string>>();
  const coverage = [];
  let tombstones = 0;
  for (const record of records.filter((item) => item.kind === "acceptance")) {
    const value = record.value;
    if (!Array.isArray(value.knowledge) || !Array.isArray(value.tombstones)) throw new Error("Invalid RSI acceptance report input");
    tombstones = value.tombstones.length;
    const cited = citations.get(String(value.experiment_id)) ?? new Set<string>();
    const links = value.knowledge.map(jsonObject);
    const groups = new Map<string, JsonObject[]>();
    for (const link of links) {
      const identity = String(link.ku_id);
      const group = groups.get(identity) ?? [];
      group.push(link);
      groups.set(identity, group);
    }
    for (const [identity, group] of groups) {
      const implemented = observed(group, "I");
      const necessary = observed(group, "V");
      counts.implemented += implemented === true ? 1 : 0;
      counts.necessary += necessary === true ? 1 : 0;
      counts.implementation_unknown += implemented === null ? 1 : 0;
      counts.necessity_unknown += necessary === null ? 1 : 0;
      counts.implemented_without_citation += implemented === true && !cited.has(identity) ? 1 : 0;
      if (necessary === true) {
        const categories = tasks.get(identity) ?? new Set<string>();
        categories.add(String(value.task_category));
        tasks.set(identity, categories);
      }
    }
    for (const identity of cited) {
      const matching = groups.get(identity) ?? [];
      counts.cited_without_implementation += matching.length > 0 && matching.every((link) => link.I === false) ? 1 : 0;
    }
    coverage.push({ experiment_id: value.experiment_id, task_id: value.task_id, holdout_task: value.holdout_task,
      coverage: value.coverage, accepted: jsonObject(value.request).accept });
  }
  return { tombstones, coverage, transfer: [...tasks.entries()].sort(([left], [right]) => compareText(left, right))
    .map(([ku_id, categories]) => ({ ku_id, target_categories: [...categories].sort(compareText),
      cross_category: categories.size >= 2, origin_category: null })) };
}

function observed(links: readonly JsonObject[], field: "I" | "V"): boolean | null {
  if (links.some((link) => link[field] === true)) return true;
  if (links.some((link) => link[field] === null)) return null;
  return false;
}
