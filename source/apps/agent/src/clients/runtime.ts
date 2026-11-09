import { createHash } from "node:crypto";
import { SCHEMA } from "../generated/runtime.ts";
import { SCHEMA as KNOWLEDGE_SCHEMA } from "../generated/knowledge-runtime.ts";
import type { KnowledgeFenceRequest, KnowledgeFenceReceipt } from "../generated/knowledge-runtime.ts";
import type { KnowledgeConsumerStatus } from "../generated/knowledge-runtime.ts";
import type {
  AccountCreateRequest, AccountObservation, AccountReceipt, AdoptCandidateRequest, BindFinalizationRequest, ContextCommit,
  FreezeRunRequest, FreezeTrialRequest, GoalPursuitBinding, KnowledgeSyncRequest, ManagedInputRequest, ManagedInputReceipt, PreparedRun, PreparedTrial,
  PrepareRunRequest, PrepareTrialRequest, ProgramBundle, ReplaceSessionRequest,
  RunLaunchManifest, RuntimeState, RuntimeStateRequest, SnapshotProgramRequest,
  SnapshotProgramResult, StopRunRequest,
} from "../generated/runtime.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { wireValidator } from "./wire-schemas.ts";
import type { EffectRpc } from "./rpc.ts";
import { validateCoherentObjectGraph } from "../workspaces/object-graph.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";

interface RuntimeValues {
  AccountCreateRequest: AccountCreateRequest;
  AccountObservation: AccountObservation;
  AccountReceipt: AccountReceipt;
  AdoptCandidateRequest: AdoptCandidateRequest;
  BindFinalizationRequest: BindFinalizationRequest;
  ContextCommit: ContextCommit;
  FreezeRunRequest: FreezeRunRequest;
  FreezeTrialRequest: FreezeTrialRequest;
  GoalPursuitBinding: GoalPursuitBinding;
  KnowledgeSyncRequest: KnowledgeSyncRequest;
  ManagedInputRequest: ManagedInputRequest;
  ManagedInputReceipt: ManagedInputReceipt;
  PreparedRun: PreparedRun;
  PreparedTrial: PreparedTrial;
  PrepareRunRequest: PrepareRunRequest;
  PrepareTrialRequest: PrepareTrialRequest;
  ProgramBundle: ProgramBundle;
  ReplaceSessionRequest: ReplaceSessionRequest;
  RunLaunchManifest: RunLaunchManifest;
  RuntimeState: RuntimeState;
  RuntimeStateRequest: RuntimeStateRequest;
  SnapshotProgramRequest: SnapshotProgramRequest;
  SnapshotProgramResult: SnapshotProgramResult;
  StopRunRequest: StopRunRequest;
}

function selectedStamp(state: RuntimeState): void {
  const { stamp, run, trial, branch, session } = state;
  if (stamp.run_id !== run.run_id || stamp.trial_id !== trial.trial_id || trial.run_id !== run.run_id ||
      branch.run_id !== run.run_id || trial.branch_id !== branch.branch_id || session.run_id !== run.run_id ||
      stamp.session.agent_session_id !== session.agent_session_id || stamp.session.revision !== session.revision ||
      stamp.trial_revision !== trial.revision || stamp.branch_revision !== branch.revision ||
      stamp.context_revision !== branch.context_revision || stamp.trial_execution_state !== trial.execution_state ||
      stamp.session_state !== session.state) {
    throw new Error("Runtime execution stamp differs from its selected Run/Trial/Branch/Session revisions");
  }
}

function coherentState(state: RuntimeState): void {
  selectedStamp(state);
  const graph = validateCoherentObjectGraph(state.graph);
  const retained = new Set(graph.objects.map((object) => canonicalKey(object)));
  const selected = [state.run, state.trial, state.branch, state.session, state.parent_program,
    ...(state.capsule === undefined ? [] : [state.capsule]), ...(state.context === undefined ? [] : [state.context])];
  if (selected.some((object) => !retained.has(canonicalKey(object)))) {
    throw new Error("Runtime current records are not exact retained graph revisions");
  }
  const retainedReferences = new Map(graph.owner_references.map(reference => [reference.ref_id, reference]));
  if (state.references.some(reference => canonicalKey(reference) !== canonicalKey(retainedReferences.get(reference.ref_id)))) {
    throw new Error("Runtime owner references differ from exact retained graph references");
  }
  const references = new Set(state.references.map(reference => reference.ref_id));
  if (state.required_ref_ids?.some(id => !references.has(id))) {
    throw new Error("Runtime required context reference is missing from its owner references");
  }
  const binding = state.trial.external_snapshot.knowledge;
  if (binding !== undefined && (binding.registration.consumer.account_id !== state.stamp.account_id ||
      state.required_ref_ids === undefined || !state.required_ref_ids.includes(binding.capture_receipt_ref_id))) {
    throw new Error("Runtime Knowledge differs from its account/required receipt binding");
  }
}

export function runtimeValue<Key extends keyof RuntimeValues>(name: Key, value: unknown): RuntimeValues[Key] {
  const snapshot = validatedSnapshot(value, wireValidator<RuntimeValues[Key]>(SCHEMA.$id, name), name);
  if (name === "RuntimeState") coherentState(snapshot as RuntimeState);
  if (name === "PreparedRun" || name === "PreparedTrial") {
    const existing = (snapshot as PreparedRun | PreparedTrial).existing_state;
    if (existing !== undefined) coherentState(existing);
  }
  return snapshot;
}

export function knowledgeConsumerStatus(value: unknown): KnowledgeConsumerStatus {
  return validatedSnapshot(value,
    wireValidator<KnowledgeConsumerStatus>(KNOWLEDGE_SCHEMA.$id, "KnowledgeConsumerStatus"), "KnowledgeConsumerStatus");
}

/** A detached typed observation, never a local source of execution authority. */
export async function readRuntime(rpc: EffectRpc, request: RuntimeStateRequest): Promise<RuntimeState> {
  const params = runtimeValue("RuntimeStateRequest", request);
  const state = runtimeValue("RuntimeState", await rpc.call("runtime_state", { ...params }));
  if (state.run.run_id !== params.run_id || state.trial.trial_id !== params.trial_id) {
    throw new Error("Runtime state identities differ from the requested Run/Trial");
  }
  return state;
}

export function knowledgeFenceRequest(value: unknown): KnowledgeFenceRequest {
  return validatedSnapshot(value,
    wireValidator<KnowledgeFenceRequest>(KNOWLEDGE_SCHEMA.$id, "KnowledgeFenceRequest"), "KnowledgeFenceRequest");
}

export function knowledgeFenceReceipt(value: unknown, request: KnowledgeFenceRequest): KnowledgeFenceReceipt {
  const receipt = validatedSnapshot(value,
    wireValidator<KnowledgeFenceReceipt>(KNOWLEDGE_SCHEMA.$id, "KnowledgeFenceReceipt"), "KnowledgeFenceReceipt");
  const requestDigest = `sha256:${createHash("sha256").update(canonicalKey(request)).digest("hex")}`;
  if (receipt.fence_id !== request.fence_id || receipt.request_digest !== requestDigest ||
      canonicalKey(receipt.registration) !== canonicalKey(request.registration)) {
    throw new Error("Knowledge fence receipt differs from its original request");
  }
  return receipt;
}
