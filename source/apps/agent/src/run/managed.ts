import type { EffectRpc } from "../clients/rpc.ts";
import { readRuntime, runtimeValue } from "../clients/runtime.ts";
import { serialRpc } from "../clients/serial-rpc.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import { SCHEMA as CONTEXT_SCHEMA } from "../generated/context.ts";
import { SCHEMA as HARNESS_SCHEMA } from "../generated/harness.ts";
import { SCHEMA as VERSION_SCHEMA } from "../generated/version-lock.ts";
import type { ContextSource } from "../generated/context.ts";
import type { HarnessInput } from "../generated/harness.ts";
import type { Program } from "../generated/objects.ts";
import type { PreparedTrial, PrepareTrialRequest, ReplaceSessionRequest, RuntimeState, SnapshotProgramResult } from "../generated/runtime.ts";
import type { UnknownIdentity } from "../generated/version-lock.ts";
import type { HarnessRunner } from "../harness/types.ts";
import { digestBytes } from "../tasks/launch-manifest.ts";
import type { LoadedManifest } from "../tasks/launch-manifest.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { managedContextCommit } from "../context/managed.ts";
import { canonicalKey } from "./scoring/canonical.ts";
import { initialGraph, runtimeId } from "./initial-graph.ts";
import { trialGraph } from "./trial-graph.ts";
import { admitNextTrial } from "./managed-search.ts";
import type { AdmittedTrial, NextTrialAdmission } from "./managed-search.ts";
import { validateTrialRequest } from "../tasks/trial-request.ts";

const contextValidator = wireValidator<ContextSource>(CONTEXT_SCHEMA.$id, "ContextSource");
const harnessValidator = wireValidator<HarnessInput>(HARNESS_SCHEMA.$id, "HarnessInput");
const evaluationValidator = wireValidator<UnknownIdentity>(VERSION_SCHEMA.$id, "UnknownIdentity");

function stateRequest(state: RuntimeState) {
  return { run_id: state.run.run_id, trial_id: state.trial.trial_id };
}

function launchedState(state: RuntimeState, loaded: LoadedManifest, trialId?: string): RuntimeState {
  const manifest = loaded.manifest;
  if (state.manifest_digest !== loaded.digest || state.run.run_id !== manifest.run_id ||
      (trialId !== undefined && state.trial.trial_id !== trialId) || state.stamp.account_id !== manifest.account_id ||
      state.run.task_contract_hash !== loaded.source.source_digest || state.run.seed !== manifest.seed ||
      state.run.root_program_id !== loaded.source.contract.baseline.program_id ||
      state.stamp.run_credit_limit !== manifest.run_credit_limit || state.trial.model_route !== manifest.model_route) {
    throw new Error("Runtime observation differs from the exact launch manifest/source/account");
  }
  return state;
}

interface PreparedSession {
  readonly state: RuntimeState;
  readonly existing: boolean;
  readonly readOnly: boolean;
  /** Present only when this invocation actually routed a new Trial preparation. */
  readonly admission?: NextTrialAdmission;
}

async function prepare(rpc: EffectRpc, loaded: LoadedManifest, requireExisting: boolean): Promise<PreparedSession> {
  const prepared = runtimeValue("PreparedRun", await rpc.call("prepare_run", {
    manifest: loaded.manifest, manifest_digest: loaded.digest,
  }));
  if (prepared.manifest_digest !== loaded.digest || prepared.intent_id !== loaded.manifest.intent_id ||
      prepared.account.account.account_id !== loaded.manifest.account_id) {
    throw new Error("Prepared manifest byte digest/account/intent mismatch");
  }
  if (prepared.existing_state !== undefined) {
    return { state: launchedState(prepared.existing_state, loaded), existing: true, readOnly: prepared.account.account.multi_trial === true };
  }
  if (requireExisting) throw new Error("Next Trial requires the original Run to be frozen first");
  const request = runtimeValue("FreezeRunRequest", {
    intent_id: loaded.manifest.intent_id, manifest_digest: loaded.digest,
    expected_account_revision: prepared.account.revision, graph: initialGraph(loaded, prepared),
  });
  const state = launchedState(runtimeValue("RuntimeState", await rpc.call("freeze_run", { ...request })), loaded, loaded.manifest.trial_id);
  return { state, existing: state.freeze_replayed === true, readOnly: state.freeze_replayed === true };
}

function verifiedTrialPreparation(prepared: PreparedTrial, request: PrepareTrialRequest): void {
  if (canonicalKey(prepared.request) !== canonicalKey(request) ||
      prepared.request_digest !== digestBytes(canonicalKey(request)) ||
      prepared.transition_stamp.run_id !== request.expected.run_id ||
      prepared.transition_stamp.trial_id !== request.expected.trial_id ||
      prepared.transition_stamp.account_id !== request.expected.account_id ||
      prepared.account.account.account_id !== request.expected.account_id) {
    throw new Error("Prepared Trial differs from its original command/transition/account");
  }
}

async function prepareNext(rpc: EffectRpc, loaded: LoadedManifest, request: PrepareTrialRequest): Promise<PreparedSession> {
  validateTrialRequest(request, loaded);
  const prepared = runtimeValue("PreparedTrial", await rpc.call("prepare_trial", { ...request }));
  verifiedTrialPreparation(prepared, request);
  if (prepared.existing_state !== undefined) {
    return { state: launchedState(prepared.existing_state, loaded, request.trial_id), existing: true, readOnly: true };
  }
  const previous = await readRuntime(rpc, { run_id: request.expected.run_id, trial_id: prepared.transition_stamp.trial_id });
  launchedState(previous, loaded, prepared.transition_stamp.trial_id);
  // The stage router admits this preparation before any Trial object, reservation
  // or model effect exists. An exact replay of a known preparation returned above
  // and is never re-routed.
  const admission = admitNextTrial(previous, request);
  if (admission.decision === "refuse") {
    throw new Error(admission.reason);
  }
  const freeze = runtimeValue("FreezeTrialRequest", {
    schema_version: "kernelcat.freeze-trial/v1", intent_id: request.intent_id, request_digest: prepared.request_digest,
    expected: prepared.transition_stamp, expected_account_revision: prepared.account.revision,
    graph: trialGraph(previous, prepared),
  });
  const state = launchedState(runtimeValue("RuntimeState", await rpc.call("freeze_trial", { ...freeze })), loaded, request.trial_id);
  return { state, existing: state.freeze_replayed === true, readOnly: state.freeze_replayed === true, admission };
}

async function replace(rpc: EffectRpc, selected: RuntimeState, request: ReplaceSessionRequest): Promise<RuntimeState> {
  const current = await readRuntime(rpc, stateRequest(selected));
  if (request.expected.run_id !== current.run.run_id || request.expected.trial_id !== current.trial.trial_id ||
      request.expected.account_id !== current.stamp.account_id) {
    throw new Error("Resume request belongs to another Run/Trial");
  }
  if (current.last_control_intent_id === request.intent_id) {
    if (canonicalKey(current.last_control_request) !== canonicalKey(request)) {
      throw new Error("Known replacement intent conflicts with its original request");
    }
  }
  // An explicit resupply of this exact request replays the original control result
  // and binds its still-current actor. A read-only state query never binds one.
  const replaced = runtimeValue("RuntimeState", await rpc.call("replace_session", { ...request }));
  if (replaced.stamp.session.agent_session_id !== request.session_id) {
    throw new Error("Replacement replay did not bind the requested current Session");
  }
  return replaced;
}

function runnable(state: RuntimeState): void {
  if (state.pending_effect_ids.length > 0 || state.stamp.run_status === "reconciling") {
    throw new Error("Run has unresolved effects; reconcile original receipts before another session or model call");
  }
  if (state.stamp.run_status !== "active" || state.stamp.session_state !== "RUNNING") {
    throw new Error(`Run cannot generate in ${state.stamp.run_status}/${state.stamp.session_state}`);
  }
}

function managedRpc(rpc: EffectRpc, selected: RuntimeState): EffectRpc {
  let catalogue: readonly string[] | undefined;
  return {
    async call(method, params = {}) {
      if (method === "complete") return await rpc.call("generation_result");
      if (method === "hello") {
        const hello = await rpc.call(method, params);
        if (!Array.isArray(hello.tools)) throw new Error("Managed hello has no tool catalogue");
        catalogue = hello.tools.map((tool: unknown) => {
          if (tool === null || typeof tool !== "object" || !("name" in tool) || typeof tool.name !== "string") {
            throw new Error("Managed hello tool catalogue is malformed");
          }
          return tool.name;
        });
        return hello;
      }
      if (method !== "model") return await rpc.call(method, params);
      const source = validatedSnapshot(params.context, contextValidator, "Managed ContextSource");
      const harness = params.harness === undefined ? undefined : validatedSnapshot(params.harness, harnessValidator, "Managed Pi input");
      const current = await readRuntime(rpc, stateRequest(selected));
      runnable(current);
      // Business Gateway actions remain distinct from native composition wrappers;
      // the full actual wrapper catalogue is retained unchanged in input_json.
      const actions = catalogue;
      if (actions === undefined) throw new Error("Managed model requires the original hello catalogue");
      const commit = managedContextCommit(current, source, harness, actions);
      return await rpc.call("model", { ...params, commit });
    },
  };
}

function candidate(state: RuntimeState, snapshot: SnapshotProgramResult): Program {
  return {
    kind: "program", program_id: snapshot.program_id, run_id: state.run.run_id, trial_id: state.trial.trial_id,
    parent_program_ids: [state.trial.parent_program_id], artifact_digest: snapshot.artifact_digest,
    configuration_digest: snapshot.configuration_digest, manifest_ref_id: snapshot.manifest_ref.ref_id,
    generation_operation_ids: [snapshot.operation_id], candidate_role: "candidate",
    native_ref_ids: [snapshot.provenance_ref.ref_id],
  };
}

function operationIntent(kind: string, loaded: LoadedManifest, state: RuntimeState): string {
  return state.trial.trial_id === loaded.manifest.trial_id ? runtimeId(kind, loaded.manifest.intent_id) :
    runtimeId(kind, loaded.manifest.intent_id, state.trial.trial_id);
}

function knownSnapshot(loaded: LoadedManifest, state: RuntimeState): SnapshotProgramResult | undefined {
  const materialization = state.materialization;
  if (materialization === undefined) return undefined;
  if (materialization.request.intent_id !== operationIntent("snapshot-intent", loaded, state) ||
      materialization.request.program_id !== runtimeId("candidate", state.run.run_id, state.trial.trial_id) ||
      materialization.request.expected.run_id !== state.run.run_id ||
      materialization.request.expected.trial_id !== state.trial.trial_id) {
    throw new Error("Original materialization belongs to a different intent or Run/Trial");
  }
  if (materialization.status !== "completed") return undefined;
  const snapshot = materialization.result;
  if (snapshot === undefined || snapshot.operation_id !== materialization.operation_id ||
      snapshot.program_id !== materialization.request.program_id) {
    throw new Error("Completed materialization is missing its original verified result");
  }
  return snapshot;
}

function confirmCandidate(loaded: LoadedManifest, state: RuntimeState): void {
  const snapshot = knownSnapshot(loaded, state);
  const adoption = state.last_candidate_adoption;
  if (snapshot === undefined || adoption === undefined ||
      adoption.intent_id !== operationIntent("candidate-intent", loaded, state) ||
      adoption.snapshot_operation_id !== snapshot.operation_id ||
      canonicalKey(adoption.program) !== canonicalKey(candidate(state, snapshot))) {
    throw new Error("Candidate-ready state lacks the original materialization/adoption identity");
  }
  const program = state.graph.objects.find((object) => object.kind === "program" &&
    object.program_id === state.trial.working_program_id);
  if (canonicalKey(program) !== canonicalKey(adoption.program)) {
    throw new Error("Adopted candidate differs from the immutable current graph Program");
  }
}

async function materialize(rpc: EffectRpc, loaded: LoadedManifest, state: RuntimeState): Promise<RuntimeState> {
  if (state.pending_effect_ids.length > 0) return state;
  let current = state;
  let snapshot = knownSnapshot(loaded, state);
  if (state.materialization === undefined) {
    const request = runtimeValue("SnapshotProgramRequest", {
      intent_id: operationIntent("snapshot-intent", loaded, state), expected: state.stamp,
      program_id: runtimeId("candidate", state.run.run_id, state.trial.trial_id),
    });
    const received = runtimeValue("SnapshotProgramResult", await rpc.call("snapshot_program", { ...request }));
    current = await readRuntime(rpc, stateRequest(state));
    snapshot = knownSnapshot(loaded, current);
    if (canonicalKey(current.materialization?.request) !== canonicalKey(request) ||
        canonicalKey(snapshot) !== canonicalKey(received)) {
      throw new Error("Materialization receipt conflicts with the original request/result");
    }
  }
  // Started/UNKNOWN/failed original work remains held. Never mint another effect.
  if (snapshot === undefined) return current;
  const adopted = runtimeValue("RuntimeState", await rpc.call("adopt_candidate", {
    intent_id: operationIntent("candidate-intent", loaded, current), expected: current.stamp,
    program: candidate(current, snapshot), snapshot_operation_id: snapshot.operation_id,
  }));
  if (adopted.trial.execution_state !== "CANDIDATES_READY") throw new Error("Owner did not adopt the candidate as CANDIDATES_READY");
  confirmCandidate(loaded, adopted);
  return adopted;
}

async function complete(
  rpc: EffectRpc, loaded: LoadedManifest, selected: RuntimeState, generation: Record<string, unknown> | undefined,
  admitted?: AdmittedTrial,
): Promise<Record<string, unknown>> {
  const completion = await rpc.call("complete");
  if (typeof completion.status !== "string") throw new Error("Owner completion has no terminal status");
  const finalState = launchedState(runtimeValue("RuntimeState", completion.runtime_state), loaded, selected.trial.trial_id);
  const settlement = finalState.pending_effect_ids.length > 0 ? "pending" : "settled";
  if (completion.settlement !== settlement) throw new Error("Owner completion settlement disagrees with its pending effects");
  validatedSnapshot(completion.evaluation, evaluationValidator, "Owner evaluation qualification");
  return {
    ...completion, run_id: finalState.run.run_id, trial_id: finalState.trial.trial_id,
    ...(completion.generation === undefined && generation !== undefined ? { generation } : {}),
    execution_state: finalState.trial.execution_state, settlement_state: finalState.trial.settlement_state,
    pending_effect_ids: finalState.pending_effect_ids,
    candidate_program_id: finalState.trial.execution_state === "CANDIDATES_READY" ? finalState.trial.working_program_id : null,
    // A routed proposal is reported, never treated as acceptance or reward.
    ...(admitted === undefined ? {} : {
      search_proposal: admitted.proposal, search_unobserved: admitted.unobserved,
    }),
  };
}

export interface ManagedRunHooks {
  /** The original serial connection is bound to this admitted current Session. */
  readonly onReady?: (connection: { readonly rpc: EffectRpc; readonly state: RuntimeState }) => void | Promise<void>;
  readonly onState?: (state: RuntimeState) => void;
  readonly onGenerationStopped?: () => void;
}

/** Native Pi owns the only conversational loop; this function adopts finite observations. */
export async function runManaged(
  client: EffectRpc, loaded: LoadedManifest, execute: HarnessRunner, resume?: ReplaceSessionRequest, nextTrial?: PrepareTrialRequest,
  hooks: ManagedRunHooks = {},
): Promise<Record<string, unknown>> {
  const rpc = serialRpc(client);
  if (resume !== undefined && nextTrial !== undefined) throw new Error("Choose exactly one of --resume and --next-trial");
  let prepared = await prepare(rpc, loaded, nextTrial !== undefined);
  if (nextTrial !== undefined) prepared = await prepareNext(rpc, loaded, nextTrial);
  let state = prepared.state;
  hooks.onState?.(state);
  const admitted: AdmittedTrial | undefined =
    prepared.admission?.decision === "admit" ? prepared.admission : undefined;
  let connectionBound = !prepared.existing && state.stamp.run_status === "active";
  if (state.trial.execution_state === "CANDIDATES_READY") {
    confirmCandidate(loaded, state);
    return await complete(rpc, loaded, state, undefined, admitted);
  }
  if (prepared.readOnly && resume === undefined) return await complete(rpc, loaded, state, undefined, admitted);
  // Pending IDs do not reveal phase; only the owner may cancel unstarted work.
  if (resume !== undefined) {
    state = launchedState(await replace(rpc, state, resume), loaded, state.trial.trial_id);
    connectionBound = state.stamp.run_status === "active";
  }
  if (state.pending_effect_ids.length > 0 || state.stamp.run_status === "reconciling") {
    return await complete(rpc, loaded, state, undefined, admitted);
  }
  let generation: Record<string, unknown> | undefined;
  if (state.stamp.run_status === "active") {
    runnable(state);
    if (prepared.existing && resume === undefined) {
      throw new Error("Existing active Run requires an explicit stable --resume request for a fresh in-memory Pi session");
    }
    if (resume === undefined && state.session.agent_session_id !== (nextTrial?.session_id ?? loaded.manifest.session_id)) {
      throw new Error("Current Session differs from manifest; use the original explicit --resume request");
    }
    await hooks.onReady?.({ rpc, state });
    try {
      generation = await execute(managedRpc(rpc, state));
    } finally {
      hooks.onGenerationStopped?.();
    }
    state = await readRuntime(rpc, stateRequest(state));
    hooks.onState?.(state);
  }
  if (state.stamp.run_status === "generation_stopped") {
    if (!connectionBound) {
      const request = runtimeValue("BindFinalizationRequest", { expected: state.stamp });
      state = runtimeValue("RuntimeState", await rpc.call("bind_finalization", { ...request }));
    }
    state = await materialize(rpc, loaded, state);
  }
  return await complete(rpc, loaded, state, generation, admitted);
}
