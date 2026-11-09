import type {
  AgentSession, Branch, LabObject, Lease, ObjectGraph, ProposalRevision,
  Reservation, TaskRun, Trial,
} from "../generated/objects.ts";
import type { PreparedRun } from "../generated/runtime.ts";
import type { LoadedManifest } from "../tasks/launch-manifest.ts";
import { digestBytes } from "../tasks/launch-manifest.ts";
import { validateCoherentObjectGraph, validateTrialKnowledge } from "../workspaces/object-graph.ts";

/** Length-bounded IDs retain the caller's stable intent through process loss. */
export function runtimeId(kind: string, ...identity: readonly (string | number)[]): string {
  return `${kind}:${digestBytes(JSON.stringify(identity)).slice(7)}`;
}

function reservations(runId: string, trialId: string): readonly Reservation[] {
  const runReservation = runtimeId("reservation", runId);
  return [
    { kind: "reservation", reservation_id: runReservation, revision: 1,
      owner_kind: "task_run", owner_id: runId, operation_ids: [], reserved: { credits: 0 },
      usage_revisions: [], settlement_state: "PENDING" },
    { kind: "reservation", reservation_id: runtimeId("reservation", runId, trialId), revision: 1,
      owner_kind: "trial", owner_id: trialId, parent_reservation_id: runReservation,
      operation_ids: [], reserved: { credits: 0 }, usage_revisions: [], settlement_state: "PENDING" },
  ];
}

function leases(prepared: PreparedRun, runId: string, branchId: string, sessionId: string): readonly Lease[] {
  const common = { kind: "lease" as const, revision: 1, epoch: 1,
    deadline: prepared.workspace_lease_deadline, heartbeat_sequence: 0,
    allocation_state: "active" as const, stop_confirmed: false };
  return [
    { ...common, lease_id: runtimeId("branch-lease", runId, branchId), resource_id: prepared.branch_resource_id,
      deadline: prepared.branch_lease_deadline,
      owner_kind: "branch", owner_id: branchId },
    { ...common, lease_id: runtimeId("workspace-lease", runId, sessionId), resource_id: prepared.workspace_resource_id,
      owner_kind: "agent_session", owner_id: sessionId },
  ];
}

function taskRun(loaded: LoadedManifest, prepared: PreparedRun): TaskRun {
  const manifest = loaded.manifest;
  return {
    kind: "task_run", run_id: manifest.run_id, host_task_id: prepared.host_task_id,
    task_id: loaded.source.contract.contract_id, task_contract_hash: loaded.source.source_digest,
    version_lock_digest: prepared.version_lock.version_lock_digest,
    root_program_id: prepared.parent_program.program_id, kb_version: prepared.kb_version,
    harness_version: prepared.harness_version, environment_version: prepared.environment_version,
    policy_digest: prepared.policy_digest, reservation_id: runtimeId("reservation", manifest.run_id), seed: manifest.seed,
  };
}

function initialTrial(loaded: LoadedManifest, prepared: PreparedRun, run: TaskRun): Trial {
  const manifest = loaded.manifest;
  return {
    kind: "trial", trial_id: manifest.trial_id, revision: 1, run_id: run.run_id,
    branch_id: manifest.branch_id, task_contract_hash: run.task_contract_hash,
    version_lock_digest: run.version_lock_digest, parent_program_id: run.root_program_id,
    proposal_revision: { proposal_id: manifest.proposal.proposal_id, revision: 1 },
    external_snapshot: {
      kb_version: prepared.kb_version, harness_version: prepared.harness_version,
      memory_revision: { memory_id: prepared.memory.memory_id, revision: prepared.memory.revision },
      memory_epoch: prepared.memory.memory_epoch, evidence_cutoff: prepared.memory.event_cursor,
      ...(prepared.knowledge === undefined ? {} : { knowledge: prepared.knowledge }),
    },
    policy_digest: run.policy_digest, model_route: manifest.model_route,
    reservation_id: runtimeId("reservation", run.run_id, manifest.trial_id),
    working_program_id: run.root_program_id, trial_feedback_cursor: 0,
    execution_state: "GENERATING", settlement_state: "PENDING", reward_revisions: [],
  };
}

function initialRecords(loaded: LoadedManifest, prepared: PreparedRun): readonly LabObject[] {
  const manifest = loaded.manifest;
  const run = taskRun(loaded, prepared);
  const trial = initialTrial(loaded, prepared, run);
  const branch: Branch = {
    kind: "branch", branch_id: manifest.branch_id, revision: 1, run_id: run.run_id,
    initial_parent_program_id: run.root_program_id, active_parent_program_id: run.root_program_id,
    state: "ACTIVE", lease_id: runtimeId("branch-lease", run.run_id, manifest.branch_id), context_revision: 0,
  };
  const session: AgentSession = {
    kind: "agent_session", agent_session_id: manifest.session_id, revision: 1, run_id: run.run_id,
    provider_thread_id: { state: "unknown", reason: "Fresh in-memory Pi session has no observed provider thread." },
    provider_root_id: { state: "unknown", reason: "Fresh in-memory Pi session has no observed provider root." },
    adapter_version: { state: "unknown", reason: "Pi adapter has not started before the initial freeze." },
    workspace_lease_id: runtimeId("workspace-lease", run.run_id, manifest.session_id),
    active_trial: { trial_id: trial.trial_id, revision: trial.revision }, state: "RUNNING",
  };
  const proposal: ProposalRevision = {
    kind: "proposal_revision", proposal_id: manifest.proposal.proposal_id, revision: 1,
    run_id: run.run_id, conditions: manifest.proposal.conditions, operation: manifest.proposal.operation,
    expected_outcome: manifest.proposal.expected_outcome, stop_conditions: manifest.proposal.stop_conditions,
    parameter_space_ref_id: prepared.parameter_ref_id,
  };
  return [run, proposal, trial, branch, session, prepared.memory, prepared.parent_program,
    ...leases(prepared, run.run_id, manifest.branch_id, manifest.session_id), ...reservations(run.run_id, trial.trial_id)];
}

/** O(N log N) supplied-graph validation; all retained root-origin records stay intact. */
export function initialGraph(loaded: LoadedManifest, prepared: PreparedRun): ObjectGraph {
  if (prepared.manifest_digest !== loaded.digest || prepared.intent_id !== loaded.manifest.intent_id ||
      prepared.account.account.account_id !== loaded.manifest.account_id) {
    throw new Error("Prepared Run does not match the loaded manifest/account intent");
  }
  const root = prepared.parent_program;
  if (root.program_id !== loaded.source.contract.baseline.program_id ||
      root.artifact_digest !== loaded.source.contract.baseline.artifact_digest) {
    throw new Error("Prepared parent differs from the frozen TaskContract baseline");
  }
  if (prepared.knowledge !== undefined) {
    validateTrialKnowledge(prepared.knowledge, loaded.manifest.run_id, loaded.manifest.trial_id,
      new Map(prepared.references.map(reference => [reference.ref_id, reference])), prepared.account.account);
    if (prepared.required_ref_ids === undefined || !prepared.required_ref_ids.includes(prepared.knowledge.capture_receipt_ref_id)) {
      throw new Error("Prepared Run omits the required Knowledge capture receipt reference");
    }
  }
  return validateCoherentObjectGraph({
    schema_version: "kernelcat.lab-objects/v1",
    provenance: loaded.source.contract.provenance,
    task_contract_sources: [loaded.source, ...prepared.retained_sources],
    version_locks: [prepared.version_lock, ...prepared.retained_version_locks],
    owner_references: prepared.references,
    objects: [...prepared.retained_objects, ...initialRecords(loaded, prepared)],
  });
}
