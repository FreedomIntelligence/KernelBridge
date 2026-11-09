import type { AgentSession, Branch, LabObject, Lease, ObjectGraph, ProposalRevision, Reservation, Trial } from "../generated/objects.ts";
import type { PreparedTrial, RuntimeState } from "../generated/runtime.ts";
import { canonicalKey } from "./scoring/canonical.ts";
import { digestBytes } from "../tasks/launch-manifest.ts";
import { validateCoherentObjectGraph, validateTrialKnowledge } from "../workspaces/object-graph.ts";
import { runtimeId } from "./initial-graph.ts";

function trialRecord(previous: RuntimeState, prepared: PreparedTrial): Trial {
  const { request, memory } = prepared;
  const run = previous.run;
  return {
    kind: "trial", trial_id: request.trial_id, revision: 1, run_id: run.run_id, branch_id: request.branch_id,
    task_contract_hash: run.task_contract_hash, version_lock_digest: run.version_lock_digest,
    parent_program_id: prepared.parent_program.program_id,
    proposal_revision: { proposal_id: request.proposal.proposal_id, revision: 1 },
    external_snapshot: {
      kb_version: run.kb_version, harness_version: run.harness_version,
      memory_revision: { memory_id: memory.memory_id, revision: memory.revision },
      memory_epoch: memory.memory_epoch, evidence_cutoff: memory.event_cursor,
      ...(prepared.knowledge === undefined ? {} : { knowledge: prepared.knowledge }),
    },
    policy_digest: run.policy_digest, model_route: previous.trial.model_route,
    reservation_id: runtimeId("reservation", run.run_id, request.trial_id),
    working_program_id: prepared.parent_program.program_id, trial_feedback_cursor: 0,
    execution_state: "GENERATING", settlement_state: "PENDING", reward_revisions: [],
  };
}

function newSession(prepared: PreparedTrial, trial: Trial): AgentSession {
  return {
    kind: "agent_session", agent_session_id: prepared.request.session_id, revision: 1, run_id: trial.run_id,
    provider_thread_id: { state: "unknown", reason: "Fresh in-memory Pi session has no observed provider thread." },
    provider_root_id: { state: "unknown", reason: "Fresh in-memory Pi session has no observed provider root." },
    adapter_version: { state: "unknown", reason: "Pi adapter has not started before this Trial freeze." },
    workspace_lease_id: runtimeId("workspace-lease", trial.run_id, prepared.request.session_id),
    active_trial: { trial_id: trial.trial_id, revision: 1 }, state: "RUNNING",
  };
}

function newLeases(prepared: PreparedTrial, branch: Branch, session: AgentSession): readonly Lease[] {
  const common = { kind: "lease" as const, revision: 1, epoch: 1, heartbeat_sequence: 0,
    allocation_state: "active" as const, stop_confirmed: false };
  return [
    { ...common, lease_id: branch.lease_id, resource_id: prepared.branch_resource_id,
      owner_kind: "branch", owner_id: branch.branch_id, deadline: prepared.branch_lease_deadline },
    { ...common, lease_id: session.workspace_lease_id, resource_id: prepared.workspace_resource_id,
      owner_kind: "agent_session", owner_id: session.agent_session_id, deadline: prepared.workspace_lease_deadline },
  ];
}

function newRecords(previous: RuntimeState, prepared: PreparedTrial): readonly LabObject[] {
  const trial = trialRecord(previous, prepared);
  const session = newSession(prepared, trial);
  const proposal: ProposalRevision = {
    kind: "proposal_revision", proposal_id: prepared.request.proposal.proposal_id, revision: 1,
    run_id: trial.run_id, conditions: prepared.request.proposal.conditions, operation: prepared.request.proposal.operation,
    expected_outcome: prepared.request.proposal.expected_outcome, stop_conditions: prepared.request.proposal.stop_conditions,
    parameter_space_ref_id: prepared.parameter_ref_id,
  };
  const branch: Branch = {
    kind: "branch", branch_id: prepared.request.branch_id, revision: 1, run_id: trial.run_id,
    initial_parent_program_id: trial.parent_program_id, active_parent_program_id: trial.parent_program_id,
    state: "ACTIVE", lease_id: runtimeId("branch-lease", trial.run_id, prepared.request.branch_id), context_revision: 0,
  };
  const reservation: Reservation = {
    kind: "reservation", reservation_id: trial.reservation_id, revision: 1, owner_kind: "trial", owner_id: trial.trial_id,
    parent_reservation_id: previous.run.reservation_id, operation_ids: [], reserved: { credits: 0 },
    usage_revisions: [], settlement_state: "PENDING",
  };
  return [proposal, trial, branch, session, prepared.memory, ...newLeases(prepared, branch, session), reservation];
}

/** O(N log N) validation over retained history plus one bounded new Trial proposal. */
export function trialGraph(previous: RuntimeState, prepared: PreparedTrial): ObjectGraph {
  if (canonicalKey(previous.stamp) !== canonicalKey(prepared.transition_stamp) ||
      previous.run.run_id !== prepared.request.expected.run_id ||
      previous.stamp.account_id !== prepared.account.account.account_id ||
      prepared.request_digest !== digestBytes(canonicalKey(prepared.request))) {
    throw new Error("Prepared Trial differs from the exact stopped transition/account/request");
  }
  if (prepared.knowledge !== undefined) {
    validateTrialKnowledge(prepared.knowledge, previous.run.run_id, prepared.request.trial_id,
      new Map(prepared.references.map(reference => [reference.ref_id, reference])), prepared.account.account);
    if (!prepared.required_ref_ids.includes(prepared.knowledge.capture_receipt_ref_id)) {
      throw new Error("Prepared Trial omits its required Knowledge capture receipt reference");
    }
  }
  const references = new Map(previous.graph.owner_references.map(reference => [reference.ref_id, reference]));
  for (const reference of prepared.references) {
    const retained = references.get(reference.ref_id);
    if (retained !== undefined && canonicalKey(retained) !== canonicalKey(reference)) {
      throw new Error("Prepared Trial changed a retained owner reference");
    }
    references.set(reference.ref_id, reference);
  }
  const parent = previous.graph.objects.find(row => row.kind === "program" && row.program_id === prepared.parent_program.program_id);
  if (canonicalKey(parent) !== canonicalKey(prepared.parent_program)) throw new Error("Prepared Trial parent is not retained in this Run");
  return validateCoherentObjectGraph({
    ...previous.graph, owner_references: [...references.values()],
    objects: [...previous.graph.objects, ...newRecords(previous, prepared)],
  });
}
