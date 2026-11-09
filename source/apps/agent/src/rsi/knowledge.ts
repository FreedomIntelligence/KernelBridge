import { Ajv2020 } from "ajv/dist/2020.js";
import type { EffectRpc } from "../clients/rpc.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { SCHEMA as QUERY_SCHEMA } from "../generated/knowledge-query.ts";
import type { KnowledgeSearchResult } from "../generated/knowledge-query.ts";
import type { JsonObject, KnowledgeCandidate, RsiEffectRequest, RsiStatus } from "../generated/rsi.ts";
import { canonicalKey, compareText } from "../run/scoring/canonical.ts";
import { artifactDigest } from "./artifacts.ts";
import { jsonObject, readRsi, stringList, text } from "./records.ts";

const queryValidator = new Ajv2020({ strict: true, ownProperties: true }).compile<KnowledgeSearchResult>({
  ...QUERY_SCHEMA, $ref: "#/$defs/KnowledgeSearchResult",
});
const externalClasses = new Set(["official_source", "framework_semantics"]);

class KnowledgeReads {
  readonly rpc: EffectRpc;
  readonly turn: string;
  private readonly cache = new Map<string, JsonObject>();
  private count = 0;

  constructor(rpc: EffectRpc, turn: string) {
    this.rpc = rpc;
    this.turn = turn;
  }

  async read(tool: RsiEffectRequest["tool"], arguments_: JsonObject): Promise<JsonObject> {
    const identity = `${this.turn}/knowledge/${artifactDigest({ tool, arguments: arguments_ })}`;
    const cached = this.cache.get(identity);
    if (cached) return cached;
    if (this.count >= 128) throw new Error("RSI Knowledge provenance exceeds 128 bounded owner reads");
    this.count += 1;
    const response = jsonObject(await this.rpc.call("rsi_effect", { identity, tool, arguments: arguments_ }));
    if (response.status !== "completed") throw new Error(`RSI ${tool} outcome is ${String(response.status)}`);
    const data = jsonObject(response.data);
    if (tool !== "knowledge_search" && data.status !== "ok") throw new Error(`RSI ${tool} owner read failed`);
    this.cache.set(identity, data);
    return data;
  }
}

async function provenance(reads: KnowledgeReads, card: JsonObject): Promise<Pick<KnowledgeCandidate,
  "source_ids" | "source_kind" | "generation" | "origin_task" | "dependencies">> {
  const dependencies = stringList(card.depends_on, "card dependencies");
  const evidence = stringList(card.evidence_refs, "card evidence");
  const sources = new Map<string, JsonObject>();
  for (const identity of evidence) {
    const data = await reads.read("knowledge_read", { evidence_id: identity });
    const sourceId = text(jsonObject(data.result).source_id, "verified evidence source");
    const registered = await reads.read("knowledge_source", { source_id: sourceId });
    const source = jsonObject(registered.source);
    if (source.source_id !== sourceId) throw new Error("RSI registered source identity differs from verified evidence");
    sources.set(sourceId, source);
  }
  const knownExternal = sources.size > 0 && dependencies.length === 0
    && [...sources.values()].every((source) => source.status === "active" && externalClasses.has(String(source.source_class)));
  const generated = [...sources.values()].some((source) => ["generated", "agent_experience"].includes(String(source.source_class)));
  return { source_ids: [...sources.keys()].sort(compareText), source_kind: knownExternal ? "external" : generated ? "generated" : "unknown",
    generation: knownExternal ? 0 : null, origin_task: null, dependencies };
}

function candidateStatus(card: JsonObject, mandatory: boolean): KnowledgeCandidate["status"] {
  if (card.card_type === "Tombstone") return "tombstone";
  if (["retracted", "superseded"].includes(String(card.status))) return "demoted";
  if (["needs_revalidation", "review_required"].includes(String(card.status))) return "review";
  if (card.status === "active" || mandatory) return "promoted";
  if (["draft", "extracted", "validated", "publishable"].includes(String(card.status))) return "candidate";
  return "unknown";
}

function projectionCards(result: KnowledgeSearchResult): readonly { card: JsonObject; mandatory: boolean }[] {
  const projected = jsonObject(result.projection);
  const groups = ["must_obey", "recommended_policies", "do_not_retry", "measurement_rules"];
  return groups.flatMap((group) => {
    const values = projected[group];
    if (!Array.isArray(values) || values.length > 100) throw new Error(`Invalid RSI Knowledge projection ${group}`);
    return values.map((card) => ({ card: jsonObject(card), mandatory: group !== "recommended_policies" }));
  });
}

export async function queryKnowledge(
  rpc: EffectRpc, status: RsiStatus, turn: string,
): Promise<{ candidates: readonly KnowledgeCandidate[]; query: JsonObject | null; result: JsonObject | null }> {
  const query = status.config.knowledge_query;
  if (query === null) return { candidates: [], query, result: null };
  const reads = new KnowledgeReads(rpc, turn);
  const response = await reads.read("knowledge_search", { request: query });
  const result = validatedSnapshot(response, queryValidator, "RSI Knowledge result");
  const ranked = new Map(result.receipt.returned.map((card) => [card.revision_id, card]));
  if (ranked.size !== result.receipt.returned.length) throw new Error("RSI Knowledge query repeats a revision");
  const candidates: KnowledgeCandidate[] = [];
  for (const { card, mandatory } of projectionCards(result)) {
    const identity = text(card.revision_id, "card revision");
    const rank = ranked.get(identity);
    if (!rank || rank.card_id !== card.card_id) throw new Error("RSI Knowledge projection differs from its ranked receipt");
    const audited = jsonObject((await reads.read("knowledge_show", { knowledge_id: identity, view: "audit" })).object);
    if (audited.revision_id !== identity || audited.card_id !== card.card_id) throw new Error("RSI Knowledge audit changed revision");
    const origin = await provenance(reads, audited);
    const exposures = status.exposures[identity] ?? 0;
    candidates.push(readRsi<KnowledgeCandidate>({ ku_id: identity, text: canonicalKey(card), ...origin,
      status: candidateStatus(audited, mandatory), mandatory, rank: rank.rank, exposures,
      new_external: origin.source_kind === "external" && exposures === 0
        && (status.exposure_unknown[identity] ?? 0) === 0 }, "KnowledgeCandidate"));
  }
  if (new Set(candidates.map((candidate) => candidate.ku_id)).size !== candidates.length || candidates.length !== ranked.size) {
    throw new Error("RSI Knowledge result membership differs from its receipt");
  }
  return { candidates, query, result: jsonObject(result) };
}
