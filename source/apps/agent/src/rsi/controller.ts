import type { EffectRpc } from "../clients/rpc.ts";
import { observeRounds } from "../loopx/round-control.ts";
import type {
  AcceptanceRequest, AdapterRequest, AdapterResult, ChangeUnit, Complexity, ConfirmationRequest,
  Decomposition, ExperimentProposal, JsonObject, KnowledgeSelection, PairedMeasurement, PrivateRecord,
  ReductionPlan, ReductionRequest, RsiCandidates, RsiInvalidation, RsiStatus, SourceFile,
} from "../generated/rsi.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";
import { artifactDigest, readDecomposition, removeUnits } from "./artifacts.ts";
import { discoveryFeedback } from "./feedback.ts";
import { queryKnowledge } from "./knowledge.ts";
import { captureMeasuredNodes } from "./lineage.ts";
import { invalidatedMeasurements, knowledgeMemory } from "./memory.ts";
import { measurementDecision, readMeasurement } from "./necessity.ts";
import { jsonObject, readRsi, Records, sourceFiles, text } from "./records.ts";
import { acceptsConfirmation, prefersUnitRemoval } from "./reduction.ts";
import { selectKnowledge } from "./selection.ts";
import {
  coverageDifference, knowledgeLinks, readTombstones, readUnitRecords, retainedTombstones, updatedUnits,
} from "./tracking.ts";
import type { UnitMeasurement, UnitRecord } from "./tracking.ts";

interface Proposal {
  readonly experiment_id: string;
  readonly proposal: ExperimentProposal;
  readonly candidate: readonly SourceFile[];
  readonly integration: boolean;
  readonly decomposition?: Decomposition;
}

interface PlanState {
  readonly plan: ReductionPlan;
  readonly baseline: readonly SourceFile[];
  readonly candidate: readonly SourceFile[];
}

function proposalRecord(record: PrivateRecord): Proposal {
  const value = record.value;
  if (typeof value.integration !== "boolean") throw new Error("RSI proposal lacks an integration identity");
  const identity = text(value.experiment_id, "experiment identity");
  if (identity !== record.record_id) throw new Error("RSI experiment identity differs from original record");
  return { experiment_id: identity, proposal: readRsi<ExperimentProposal>(value.proposal, "ExperimentProposal"),
    candidate: sourceFiles(value.candidate), integration: value.integration,
    ...(value.integration ? { decomposition: readRsi<Decomposition>(value.decomposition, "Decomposition") } : {}) };
}

function planRecord(record: PrivateRecord): PlanState {
  return { plan: readRsi<ReductionPlan>(record.value.plan, "ReductionPlan"),
    baseline: sourceFiles(record.value.baseline), candidate: sourceFiles(record.value.candidate) };
}

function unitMeasurements(records: readonly PrivateRecord[], experiment: string): readonly UnitMeasurement[] {
  return records.filter((record) => record.value.experiment_id === experiment && record.value.confirmation === false)
    .map((record) => ({ request: readRsi<ReductionRequest>(record.value.request, "ReductionRequest"),
      measurement: readMeasurement(record.value.measurement) }));
}

/** Protected outer RSI decisions, invoked through finite Pi lifecycle callbacks. */
class RsiController {
  readonly rpc: EffectRpc;
  readonly records: Records;
  private status: RsiStatus | null = null;
  private readonly applicationBaseline?: () => Promise<{ baseline_record_id: string; record_sequence: number } | undefined>;

  constructor(rpc: EffectRpc, applicationBaseline?: () => Promise<{ baseline_record_id: string; record_sequence: number } | undefined>) {
    this.rpc = rpc;
    this.applicationBaseline = applicationBaseline;
    this.records = new Records(rpc);
  }

  async call(method: string, params: Readonly<Record<string, unknown>> = {}): Promise<Record<string, unknown>> {
    if (["rsi_prepare", "rsi_pending", "rsi_checkpoint"].includes(method) && Object.keys(params).length > 0) {
      throw new Error(`${method} takes no parameters`);
    }
    switch (method) {
      case "rsi_prepare": return this.prepare();
      case "rsi_select": return this.select(params);
      case "rsi_pending": return this.pending();
      case "rsi_checkpoint": return this.checkpoint();
      case "rsi_invalidate": return this.invalidate(params);
      case "rsi_measure": return { ...await this.measure(params, false) };
      case "rsi_confirm": return { ...await this.measure(params, true) };
      case "rsi_accept": return this.accept(params);
      default: throw new Error(`Unknown RSI controller operation: ${method}`);
    }
  }

  private current(): RsiStatus {
    if (this.status === null) throw new Error("RSI turn has not been prepared");
    return this.status;
  }

  private turnId(): string {
    const status = this.current();
    return `${status.task_id}/rsi/turn/${status.step}`;
  }

  private async refresh(): Promise<RsiStatus> {
    const status = readRsi<RsiStatus>(await this.rpc.call("rsi_status", {}), "RsiStatus");
    if (new Set(status.config.footprints.map((rule) => rule.ku_id)).size !== status.config.footprints.length) {
      throw new Error("RSI configuration repeats a footprint rule identity");
    }
    if (this.status !== null && (status.task_id !== this.status.task_id
        || canonicalKey(status.config) !== canonicalKey(this.status.config))) {
      throw new Error("RSI task or operator configuration changed during the session");
    }
    await this.records.refresh();
    this.records.applicationBaseline = await this.applicationBaseline?.();
    if (status.record_sequence !== this.records.sequence) throw new Error("RSI status is stale relative to original records");
    this.status = status;
    return status;
  }

  private async prepare(): Promise<Record<string, unknown>> {
    const status = await this.refresh();
    await captureMeasuredNodes(this.records, this.rpc);
    await observeRounds(this.records, this.rpc, status.task_id);
    if (status.stop) return { stop: true };
    const initialId = `${status.task_id}/rsi/initial`;
    if (this.records.get(initialId) === undefined) {
      await this.records.write(initialId, "initial", { source: status.files, config_digest: artifactDigest(status.config) });
    }
    const initial = this.records.get(initialId)!;
    if (initial.value.config_digest !== artifactDigest(status.config)) throw new Error("RSI configuration differs from its frozen initial state");
    const identity = `${this.turnId()}/candidates`;
    const prior = this.records.get(identity);
    if (prior) return { ...readRsi<RsiCandidates>(prior.value.candidates, "RsiCandidates") };
    const knowledge = await queryKnowledge(this.rpc, status, this.turnId());
    const memory = knowledgeMemory(this.records, status, knowledge.candidates);
    const candidates = readRsi<RsiCandidates>({ turn_id: this.turnId(), policy: status.config.policy,
      candidates: [...knowledge.candidates, ...memory.items.map((item) => item.candidate)] }, "RsiCandidates");
    await this.records.write(identity, "candidates", { candidates, query: knowledge.query, result: knowledge.result,
      memory, exposures: status.exposures, exposure_unknown: status.exposure_unknown });
    return { ...candidates };
  }

  private async checkpoint(): Promise<Record<string, unknown>> {
    const status = await this.refresh();
    const captured = await captureMeasuredNodes(this.records, this.rpc);
    await observeRounds(this.records, this.rpc, status.task_id);
    return { captured };
  }

  private async invalidate(value: unknown): Promise<Record<string, unknown>> {
    const request = readRsi<RsiInvalidation>(value, "RsiInvalidation");
    const status = await this.refresh();
    if (!this.records.hasMeasurement(request.measurement_id)) {
      throw new Error("RSI invalidation requires an original measurement in this task");
    }
    const identity = `${status.task_id}/rsi/invalidation/${artifactDigest(request.measurement_id)}`;
    return { ...await this.records.write(identity, "invalidation", request) };
  }

  private async select(value: unknown): Promise<Record<string, unknown>> {
    const selection = readRsi<KnowledgeSelection>(value, "KnowledgeSelection");
    await this.records.refresh();
    const candidates = this.records.get(`${this.turnId()}/candidates`);
    if (!candidates) throw new Error("RSI selection has no original candidates");
    if ((this.records.last("invalidation")?.sequence ?? 0) > candidates.sequence) {
      throw new Error("RSI knowledge was invalidated after context preparation; a fresh turn boundary is required");
    }
    if (canonicalKey(selectKnowledge(candidates.value.candidates)) !== canonicalKey(selection)) {
      throw new Error("RSI selection differs from its deterministic admitted policy");
    }
    const pool = readRsi<RsiCandidates>(candidates.value.candidates, "RsiCandidates");
    const byId = new Map(pool.candidates.map((candidate) => [candidate.ku_id, candidate]));
    const context = selection.selected.map((item) => ({ ku_id: item.ku_id, text: byId.get(item.ku_id)!.text }));
    const recordId = `${this.turnId()}/selection`;
    await this.records.write(recordId, "selection", { selection, context });
    return this.rpc.call("rsi_context", { turn_id: this.turnId(), record_id: recordId });
  }

  private baseline(): readonly SourceFile[] {
    const record = this.records.baseline();
    return sourceFiles(record.value.source);
  }

  private priorUnits(): readonly UnitRecord[] {
    const previous = this.records.baseline();
    if (previous.kind !== "acceptance") return [];
    const units = readUnitRecords(previous.value.units);
    const invalid = invalidatedMeasurements(this.records);
    const confirmationInvalid = previous.value.confirmation === undefined
      || invalid.has(readMeasurement(previous.value.confirmation).measurement_id);
    const candidateRecord = this.records.get(`${this.turnId()}/candidates`);
    const candidates = candidateRecord
      ? readRsi<RsiCandidates>(candidateRecord.value.candidates, "RsiCandidates").candidates : [];
    const revoked = new Set(candidates.filter((item) => ["review", "demoted", "tombstone"].includes(item.status)).map((item) => item.ku_id));
    return units.map((item) => item.state !== "deleted" && (confirmationInvalid
      || invalid.has(item.decision.measurement_id) || item.unit.ku_ids.some((identity) => revoked.has(identity)))
      ? { ...item, state: "review" } : item);
  }

  private tombstones(): readonly ChangeUnit[] {
    const previous = this.records.last("acceptance");
    return previous ? readTombstones(previous.value.tombstones) : [];
  }

  private async pending(): Promise<Record<string, unknown>> {
    const status = await this.refresh();
    const pending = this.records.pending();
    if (pending.length > 1) throw new Error("Multiple RSI experiments are pending in one turn");
    const proposal = pending.length === 1 ? proposalRecord(pending[0]) : await this.integration(status);
    if (proposal === null) return { pending: null };
    if (artifactDigest(status.files) !== artifactDigest(proposal.candidate)) {
      throw new Error("RSI source changed after the experiment was submitted");
    }
    return { pending: (await this.plan(proposal)).plan };
  }

  private async integration(status: RsiStatus): Promise<Proposal | null> {
    const prior = this.records.baseline();
    if (prior.kind !== "acceptance" || artifactDigest(status.files) !== artifactDigest(this.baseline())) return null;
    const periodic = (status.step + 1) % status.config.policy.integration_interval === 0;
    const files = new Map(status.files.map((file) => [file.path, file.text]));
    const eligible = this.priorUnits().filter((item) => (item.state === "review" || periodic && item.state !== "deleted")
      && item.unit.after.length > 0 && files.get(item.unit.path)?.split(item.unit.after).length === 2);
    if (eligible.length === 0) return null;
    const complexity = readRsi<Complexity>(prior.value.complexity, "Complexity");
    const identity = `${this.turnId()}/integration`;
    const record = await this.records.write(identity, "proposal", { experiment_id: identity,
      proposal: { hypothesis: "Revalidate retained units", expected_delta: 0, cites: [] },
      candidate: status.files, integration: true, decomposition: {
        units: eligible.slice(0, status.config.policy.max_units).map((item) => item.unit),
        candidate_complexity: complexity, baseline_complexity: complexity } });
    return proposalRecord(record);
  }

  private async plan(proposal: Proposal): Promise<PlanState> {
    const identity = `${proposal.experiment_id}/plan`;
    const existing = this.records.get(identity);
    if (existing) return planRecord(existing);
    const baseline = this.baseline();
    this.checkCitations(proposal);
    const split = proposal.integration ? proposal.decomposition! : readDecomposition(
      (await this.adapter(proposal.experiment_id, "decompose", baseline, proposal.candidate, baseline, "decomposition")).decomposition,
      baseline, proposal.candidate, this.current().config.policy.max_units);
    const plan = readRsi<ReductionPlan>({ ...split, task_id: this.current().task_id, experiment_id: proposal.experiment_id,
      proposal: proposal.proposal, policy: this.current().config.policy,
      baseline_digest: artifactDigest(baseline), candidate_digest: artifactDigest(proposal.candidate),
      stale_units: this.priorUnits().filter((item) => item.state === "review").map((item) => item.unit.unit_id),
      tombstones: this.tombstones(), integration_due: proposal.integration }, "ReductionPlan");
    return planRecord(await this.records.write(identity, "plan", { plan, baseline, candidate: proposal.candidate }));
  }

  private checkCitations(proposal: Proposal): void {
    const known = new Set(this.records.all("candidates").flatMap((record) =>
      readRsi<RsiCandidates>(record.value.candidates, "RsiCandidates").candidates.map((candidate) => candidate.ku_id)));
    if (proposal.proposal.cites.some((identity) => !known.has(identity))) {
      throw new Error("RSI proposal cites an unknown registered knowledge identity");
    }
  }

  private async adapter(
    experiment: string, operation: AdapterRequest["operation"], baseline: readonly SourceFile[],
    keep: readonly SourceFile[], remove: readonly SourceFile[], unit: string,
  ): Promise<AdapterResult> {
    const identity = `${experiment}/adapter/${operation}/${artifactDigest(unit)}`;
    const request: AdapterRequest = { schema_version: "kernelcat.rsi-adapter/v1", operation,
      task_id: this.current().task_id, experiment_id: experiment, unit_id: unit,
      baseline, keep, remove, sample_namespace: identity };
    const result = readRsi<AdapterResult>(await this.rpc.call("rsi_adapter", { identity, request }), "AdapterResult");
    const expected = operation === "decompose" ? "decomposition" : "measurement";
    if (!(expected in result) || Object.keys(result).some((key) => key !== expected && key !== "source_snapshot")) {
      throw new Error("RSI adapter returned the wrong result kind");
    }
    if (result.source_snapshot && (result.source_snapshot.baseline.source_digest !== artifactDigest(baseline)
        || result.source_snapshot.keep.source_digest !== artifactDigest(keep)
        || result.source_snapshot.remove.source_digest !== artifactDigest(remove))) {
      throw new Error("RSI immutable commits differ from the requested source variants");
    }
    return result;
  }

  private selectedPlan(experiment: string): PlanState {
    const record = this.records.get(`${experiment}/plan`);
    if (!record) throw new Error("RSI operation has no original frozen plan");
    const pending = this.records.pending();
    if (pending.length !== 1 || pending[0].record_id !== experiment) throw new Error("RSI operation is not for the active experiment");
    return planRecord(record);
  }

  private async measure(value: unknown, confirm: boolean): Promise<PairedMeasurement> {
    const request = confirm ? readRsi<ConfirmationRequest>(value, "ConfirmationRequest")
      : readRsi<ReductionRequest>(value, "ReductionRequest");
    const state = this.selectedPlan(request.experiment_id);
    const unit = confirm ? "confirmation" : (request as ReductionRequest).unit_id;
    if (!confirm && request.removed_unit_ids.includes(unit)) throw new Error("RSI measured unit is already removed");
    const retained = removeUnits(state.candidate, state.plan.units, request.removed_unit_ids);
    const keep = confirm ? state.baseline : retained;
    const removed = confirm ? retained : removeUnits(state.candidate, state.plan.units, [...request.removed_unit_ids, unit]);
    const identity = `${request.experiment_id}/measurement/${confirm ? "confirm" : artifactDigest(unit)}`;
    const existing = this.records.get(identity);
    if (existing) {
      if (canonicalKey(existing.value.request) !== canonicalKey(request)) throw new Error("RSI measurement request changed on replay");
      return readMeasurement(existing.value.measurement);
    }
    const result = await this.adapter(request.experiment_id,
      confirm ? "confirm" : "measure", state.baseline, keep, removed, unit);
    const measurement = readMeasurement(result.measurement);
    this.checkMeasurement(measurement, keep, removed, confirm);
    await this.records.write(identity, "measurement", { experiment_id: request.experiment_id,
      request, confirmation: confirm, measurement,
      ...(result.source_snapshot ? { source_snapshot: result.source_snapshot } : {}) });
    return measurement;
  }

  private checkMeasurement(
    measurement: PairedMeasurement, keep: readonly SourceFile[], remove: readonly SourceFile[], confirm: boolean,
  ): void {
    if (measurement.task_id !== this.current().task_id || measurement.keep_digest !== artifactDigest(keep)
        || measurement.remove_digest !== artifactDigest(remove) || measurement.independent_confirmation !== confirm
        || measurement.evidence_ids.length === 0 || measurement.sample_ids.length === 0) {
      throw new Error("RSI measurement lacks evidence or differs from its requested variants");
    }
    if (this.records.hasMeasurement(measurement.measurement_id)
        || measurement.sample_ids.some((sample) => this.records.hasSample(sample))) {
      throw new Error("RSI adapter reused an existing measurement or sample identity");
    }
  }

  private confirmation(request: AcceptanceRequest): PairedMeasurement {
    const records = this.records.experiment("measurement", request.experiment_id)
      .filter((record) => record.value.confirmation === true);
    if (records.length !== 1) throw new Error("RSI acceptance requires one original confirmation");
    const measurement = readMeasurement(records[0].value.measurement);
    const original = readRsi<ConfirmationRequest>(records[0].value.request, "ConfirmationRequest");
    if (measurement.measurement_id !== request.measurement_id
        || canonicalKey(original.removed_unit_ids) !== canonicalKey(request.removed_unit_ids)) {
      throw new Error("RSI acceptance differs from the confirmed candidate");
    }
    return measurement;
  }

  private checkDecisions(plan: ReductionPlan, request: AcceptanceRequest, samples: readonly UnitMeasurement[]): boolean {
    let eligible = true;
    const decisions = new Map(request.decisions.map((decision) => [decision.measurement_id, decision]));
    const byUnit = new Map(samples.map((sample) => [sample.request.unit_id, sample]));
    if (decisions.size !== request.decisions.length || byUnit.size !== plan.units.length || samples.length !== decisions.size) {
      throw new Error("RSI acceptance needs exactly one measured decision per unit");
    }
    for (const unit of plan.units) {
      const sample = byUnit.get(unit.unit_id);
      if (!sample) throw new Error("RSI acceptance omitted a unit");
      const decision = measurementDecision(sample.measurement, plan.policy);
      if (canonicalKey(decisions.get(decision.measurement_id)) !== canonicalKey(decision)) {
        throw new Error("RSI decision differs from its original paired measurement");
      }
      if (request.removed_unit_ids.includes(unit.unit_id) && !prefersUnitRemoval(sample.measurement, decision)) {
        throw new Error("RSI removal requires equivalence without increasing complexity");
      }
      const reintroduced = plan.tombstones.some((old) => old.anchor === unit.anchor && old.after === unit.after);
      if (request.accept && reintroduced && !request.removed_unit_ids.includes(unit.unit_id) && decision.verdict !== "necessary") {
        eligible = false;
      }
    }
    return eligible;
  }

  private async accept(value: unknown): Promise<Record<string, unknown>> {
    const request = readRsi<AcceptanceRequest>(value, "AcceptanceRequest");
    const existing = this.records.get(`${request.experiment_id}/accept`);
    if (existing) {
      if (canonicalKey(existing.value.requested ?? existing.value.request) !== canonicalKey(request)) throw new Error("RSI acceptance request changed on replay");
      const adopted = readRsi<AcceptanceRequest>(existing.value.request, "AcceptanceRequest");
      return { experiment_id: request.experiment_id, accepted: adopted.accept,
        source_digest: artifactDigest(sourceFiles(existing.value.source)), ...this.feedback(request.experiment_id) };
    }
    const state = this.selectedPlan(request.experiment_id);
    const measurement = this.confirmation(request);
    const samples = unitMeasurements(this.records.experiment("measurement", request.experiment_id), request.experiment_id);
    const eligible = this.checkDecisions(state.plan, request, samples);
    if (request.accept !== acceptsConfirmation(measurement, state.plan)) throw new Error("RSI acceptance differs from confirmation policy");
    const candidate = removeUnits(state.candidate, state.plan.units, request.removed_unit_ids);
    if (measurement.remove_digest !== artifactDigest(candidate)) throw new Error("RSI acceptance artifact differs from confirmation");
    const adopted = { ...request, accept: request.accept && eligible };
    const confirmationRecord = this.records.experiment("measurement", request.experiment_id)
      .find((item) => item.value.confirmation === true)!;
    const source = adopted.accept ? candidate : state.baseline;
    const units = updatedUnits(this.priorUnits(), state.plan, adopted, samples, measurement);
    const record = this.records.request(`${request.experiment_id}/accept`, "acceptance", {
      experiment_id: request.experiment_id, requested: request, request: adopted,
      turn_id: this.turnId(),
      rejection_reason: eligible ? null : "tombstone_requires_fresh_necessity", source, units,
      tombstones: retainedTombstones(this.tombstones(), units),
      complexity: adopted.accept ? measurement.remove_complexity : measurement.keep_complexity,
      knowledge: knowledgeLinks(state.plan, samples, request.decisions, this.current().config.footprints),
      confirmation: measurement, coverage: coverageDifference(measurement),
      ...(confirmationRecord.value.source_snapshot ? { source_snapshot: confirmationRecord.value.source_snapshot } : {}),
      task_id: this.current().task_id, task_category: this.current().config.task_category,
      holdout_task: this.current().config.holdout_task,
    });
    const result = readRsi<PrivateRecord>(await this.rpc.call("rsi_apply", {
      expected: state.candidate, replacement: source, record,
    }), "PrivateRecord");
    this.records.checkWrite(result, record);
    this.records.adopt(result);
    return { experiment_id: request.experiment_id, accepted: adopted.accept, source_digest: artifactDigest(source),
      ...this.feedback(request.experiment_id) };
  }

  private feedback(experiment: string): Record<string, unknown> {
    return {
      discovery: unitMeasurements(this.records.experiment("measurement", experiment), experiment)
        .map(sample => discoveryFeedback(sample.measurement, sample.request.unit_id)),
      feedback_scope: "Discovery only; higher log scores are better. Independent confirmation remains private. Read the current source after rejection; submission receipts do not contain evaluator logs.",
    };
  }
}

export function createRsiController(rpc: EffectRpc, applicationBaseline?: () => Promise<{ baseline_record_id: string; record_sequence: number } | undefined>): EffectRpc {
  return new RsiController(rpc, applicationBaseline);
}
