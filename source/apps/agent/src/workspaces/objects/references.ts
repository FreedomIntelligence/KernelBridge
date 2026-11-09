import type {
  AgentSession, Branch, Capsule, ContextManifest, ContextReference, Evaluation,
  ExternalSnapshot, Fact, Lease, Program, PrunePlan, ReceiptReference, ReplayJob,
  Reservation, TaskMemoryRevision, TaskRun, Trial, TrialReference, TurnContext,
} from "../../generated/objects.ts";
import type { Baseline } from "../../generated/task-contract.ts";
import {
  equal, history, label, leaseKey, programForRun, requireCoherence, resolve, revisionKey,
} from "./graph-index.ts";
import type { GraphIndex } from "./graph-index.ts";

function ownerReference(index: GraphIndex, id: string, context: string, digest?: string) {
  const reference = index.references.get(id);
  requireCoherence(reference !== undefined, context, `missing owner reference ${JSON.stringify(id)}`);
  if (digest !== undefined) {
    equal(reference.digest, digest, `${context} reference ${JSON.stringify(id)} digest`);
  }
  return reference;
}

function run(index: GraphIndex, id: string, context: string): TaskRun {
  return resolve(index, "task_run", id, context);
}

function sameRun(
  index: GraphIndex, kind: "fact" | "branch", id: string, runId: string, context: string,
): void {
  equal(resolve(index, kind, id, context).run_id, runId, `${context} ${kind} Run`);
}

function externalSnapshot(index: GraphIndex, snapshot: ExternalSnapshot, runId: string, context: string): void {
  const memory = resolve(index, "task_memory_revision", snapshot.memory_revision.memory_id, context, snapshot.memory_revision.revision);
  equal(memory.run_id, runId, `${context} memory Run`);
  equal(snapshot.memory_epoch, memory.memory_epoch, `${context} memory_epoch`);
  equal(snapshot.evidence_cutoff, memory.event_cursor, `${context} evidence_cutoff`);
}

function operation(index: GraphIndex, id: string, reservationId: string, context: string): void {
  equal(index.operations.get(id), reservationId, `${context} operation ${JSON.stringify(id)} reservation`);
}

function contractFor(index: GraphIndex, owner: TaskRun, context: string) {
  const source = index.contracts.get(owner.task_contract_hash);
  requireCoherence(source !== undefined, context, `missing TaskContract source ${owner.task_contract_hash}`);
  equal(source.contract.contract_id, owner.task_id, `${context} TaskContract ID`);
  return source.contract;
}

function lockFor(index: GraphIndex, digest: string, context: string) {
  const entry = index.locks.get(digest);
  requireCoherence(entry !== undefined, context, `missing VersionLock ${digest}`);
  return entry.lock;
}

function baseline(index: GraphIndex, value: Baseline, runId: string, context: string): void {
  const program = programForRun(index, value.program_id, runId, context);
  equal(program.artifact_digest, value.artifact_digest, `${context} artifact_digest`);
}

function taskRun(index: GraphIndex, row: TaskRun): void {
  const contract = contractFor(index, row, label(row));
  lockFor(index, row.version_lock_digest, label(row));
  const root = programForRun(index, row.root_program_id, row.run_id, label(row));
  equal(root.candidate_role, "root", `${label(row)} root role`);
  baseline(index, contract.baseline, row.run_id, `${label(row)} TaskContract scoring baseline`);
  const budget = resolve(index, "reservation", row.reservation_id, label(row));
  equal([budget.owner_kind, budget.owner_id], ["task_run", row.run_id], `${label(row)} reservation owner`);
}

function trial(index: GraphIndex, row: Trial): void {
  const owner = run(index, row.run_id, label(row));
  equal(row.task_contract_hash, owner.task_contract_hash, `${label(row)} TaskContract`);
  equal(row.version_lock_digest, owner.version_lock_digest, `${label(row)} VersionLock`);
  const lock = lockFor(index, row.version_lock_digest, label(row));
  requireCoherence(Object.hasOwn(lock.models, row.model_route), label(row), `missing pinned model route ${JSON.stringify(row.model_route)}`);
  sameRun(index, "branch", row.branch_id, row.run_id, label(row));
  const proposal = resolve(index, "proposal_revision", row.proposal_revision.proposal_id, label(row), row.proposal_revision.revision);
  equal(proposal.run_id, row.run_id, `${label(row)} proposal Run`);
  externalSnapshot(index, row.external_snapshot, row.run_id, label(row));
  const budget = resolve(index, "reservation", row.reservation_id, label(row));
  equal([budget.owner_kind, budget.owner_id], ["trial", row.trial_id], `${label(row)} reservation owner`);
}

function program(index: GraphIndex, row: Program): void {
  ownerReference(index, row.manifest_ref_id, label(row));
  for (const id of row.native_ref_ids) {
    ownerReference(index, id, label(row));
  }
  const reservationId = row.trial_id === undefined
    ? run(index, row.run_id, label(row)).reservation_id
    : resolve(index, "trial", row.trial_id, label(row)).reservation_id;
  for (const id of row.generation_operation_ids) {
    operation(index, id, reservationId, label(row));
  }
}

function evaluationPlan(index: GraphIndex, row: Evaluation): void {
  const owner = run(index, row.run_id, label(row));
  const contract = contractFor(index, owner, label(row));
  const candidate = programForRun(index, row.program_id, row.run_id, label(row));
  equal(row.artifact_digest, candidate.artifact_digest, `${label(row)} artifact_digest`);
  equal(row.plan.task_contract_hash, owner.task_contract_hash, `${label(row)} plan TaskContract`);
  equal(row.plan.casepack_digest, contract.input_domain.casepack_digest, `${label(row)} plan casepack`);
  equal(row.plan.version_lock_digest, owner.version_lock_digest, `${label(row)} plan VersionLock`);
  lockFor(index, row.plan.version_lock_digest, label(row));
  equal(row.plan.scoring_baseline, contract.baseline, `${label(row)} frozen scoring baseline`);
  baseline(index, row.plan.scoring_baseline, row.run_id, `${label(row)} scoring baseline`);
  baseline(index, row.plan.comparison_baseline, row.run_id, `${label(row)} comparison baseline`);
  ownerReference(index, row.plan.shape_ref_id, label(row));
  ownerReference(index, row.plan.measurement_protocol_ref_id, label(row));
  if (row.trial_id === undefined) {
    equal(candidate.program_id, owner.root_program_id, `${label(row)} root evaluation Program`);
    equal(row.plan.comparison_baseline, contract.baseline, `${label(row)} root comparison baseline`);
    operation(index, row.plan.operation_id, owner.reservation_id, label(row));
  } else {
    const trial = resolve(index, "trial", row.trial_id, label(row));
    equal(trial.run_id, row.run_id, `${label(row)} Trial Run`);
    requireCoherence(candidate.trial_id === trial.trial_id || candidate.program_id === trial.parent_program_id || candidate.program_id === contract.baseline.program_id, label(row), "Program is neither this Trial's candidate, pinned parent nor scoring baseline");
    equal(row.plan.comparison_baseline.program_id, trial.parent_program_id, `${label(row)} Trial comparison parent`);
    operation(index, row.plan.operation_id, trial.reservation_id, label(row));
  }
}

function replicate(index: GraphIndex, row: Evaluation): void {
  const id = row.plan.replicate_of_evaluation_id;
  if (id === undefined) {
    return;
  }
  const previous = resolve(index, "evaluation", id, `${label(row)} replicate`);
  requireCoherence(previous.evaluation_id !== row.evaluation_id && previous.plan.operation_id !== row.plan.operation_id, label(row), "replicate requires a different evaluation and operation");
  requireCoherence(previous.plan.replicate !== row.plan.replicate, label(row), "replicate identity must differ");
  equal([row.run_id, row.trial_id, row.program_id, row.artifact_digest], [previous.run_id, previous.trial_id, previous.program_id, previous.artifact_digest], `${label(row)} replicate cohort`);
  const { plan_id: _id, operation_id: _operation, replicate: _replicate, replicate_of_evaluation_id: _link, ...cohort } = row.plan;
  const { plan_id: _previousId, operation_id: _previousOperation, replicate: _previousReplicate, replicate_of_evaluation_id: _previousLink, ...previousCohort } = previous.plan;
  equal(cohort, previousCohort, `${label(row)} replicate comparison cohort`);
}

function evaluationReceipts(index: GraphIndex, row: Evaluation): void {
  const contract = contractFor(index, run(index, row.run_id, label(row)), label(row));
  for (const receipt of row.receipts) {
    const context = `${label(row)} receipt ${receipt.receipt_revision}`;
    ownerReference(index, receipt.receipt_ref_id, context, receipt.receipt_digest);
    const observation = receipt.observation;
    if (observation === undefined) {
      continue;
    }
    equal([observation.evaluation_id, observation.receipt_revision, observation.receipt_digest], [row.evaluation_id, receipt.receipt_revision, receipt.receipt_digest], `${context} observation identity`);
    equal([observation.program_id, observation.artifact_digest, observation.stage], [row.program_id, row.artifact_digest, row.plan.stage], `${context} observation Program/stage`);
    equal([observation.task_contract_digest, observation.casepack_digest], [row.plan.task_contract_hash, row.plan.casepack_digest], `${context} observation digests`);
    equal(observation.baseline, row.plan.scoring_baseline, `${context} observation baseline`);
    equal(observation.measurement_protocol, contract.measurement_protocol, `${context} observation measurement protocol`);
    equal(observation.timing, contract.timing, `${context} observation timing protocol`);
    if (row.plan.environment_version.state === "known" && typeof observation.environment === "string") {
      equal(observation.environment, row.plan.environment_version.value, `${context} environment`);
    }
  }
}

function branch(index: GraphIndex, row: Branch): void {
  run(index, row.run_id, label(row));
  programForRun(index, row.initial_parent_program_id, row.run_id, label(row));
  programForRun(index, row.active_parent_program_id, row.run_id, label(row));
  requireCoherence(index.leaseOwners.has(leaseKey(row.lease_id, "branch", row.branch_id)), label(row), `missing compatible Branch lease ${JSON.stringify(row.lease_id)}`);
}

function exactTrial(index: GraphIndex, reference: TrialReference, context: string): Trial {
  return resolve(index, "trial", reference.trial_id, context, reference.revision);
}

function exactContext(index: GraphIndex, reference: ContextReference, context: string): ContextManifest {
  return resolve(index, "context_manifest", reference.context_id, context, reference.revision);
}

function session(index: GraphIndex, row: AgentSession): void {
  run(index, row.run_id, label(row));
  requireCoherence(index.leaseOwners.has(leaseKey(row.workspace_lease_id, "agent_session", row.agent_session_id)), label(row), "missing compatible workspace lease");
  if (row.active_trial !== undefined) {
    equal(exactTrial(index, row.active_trial, label(row)).run_id, row.run_id, `${label(row)} active Trial Run`);
  }
  if (row.active_context !== undefined) {
    const context = exactContext(index, row.active_context, label(row));
    equal(exactTrial(index, context.trial, label(row)).run_id, row.run_id, `${label(row)} active Context Run`);
    if (row.active_trial !== undefined) {
      equal(context.trial, row.active_trial, `${label(row)} active Context Trial`);
    }
  }
}

function capsule(index: GraphIndex, row: Capsule): void {
  const trial = exactTrial(index, row.initial_trial, label(row));
  equal(row.parent_program_id, trial.parent_program_id, `${label(row)} initial parent`);
  equal(row.proposal_revision, trial.proposal_revision, `${label(row)} proposal`);
  equal(row.external_snapshot, trial.external_snapshot, `${label(row)} external snapshot`);
  equal(row.policy_digest, trial.policy_digest, `${label(row)} policy`);
  const context = exactContext(index, row.initial_context, label(row));
  equal(row.initial_context.revision, history(index, "context_manifest", context.context_id)[0].revision, `${label(row)} initial Context revision among supplied history`);
  equal(context.trial, row.initial_trial, `${label(row)} initial Context Trial`);
  equal(context.capsule_id, row.capsule_id, `${label(row)} initial Context Capsule`);
  equal([context.input_ref_id, context.input_digest, context.compiler_version], [row.input_ref_id, row.input_digest, row.compiler_version], `${label(row)} initial input/compiler`);
  ownerReference(index, row.input_ref_id, label(row), row.input_digest);
}

function manifest(index: GraphIndex, row: ContextManifest): void {
  const trial = exactTrial(index, row.trial, label(row));
  const capsule = resolve(index, "capsule", row.capsule_id, label(row));
  equal(capsule.initial_trial.trial_id, trial.trial_id, `${label(row)} Capsule Trial`);
  requireCoherence(capsule.initial_trial.revision <= trial.revision, label(row), "Capsule pins a later Trial revision");
  equal(row.policy_digest, trial.policy_digest, `${label(row)} policy`);
  const input = ownerReference(index, row.input_ref_id, label(row), row.input_digest);
  if (input.byte_length !== undefined) {
    equal(row.input_bytes, input.byte_length, `${label(row)} known input byte count`);
  }
  requireCoherence(row.input_bytes <= row.limit_bytes, label(row), "input_bytes exceeds limit_bytes");
  for (const id of row.dependency_ref_ids) {
    ownerReference(index, id, label(row));
  }
  for (const item of row.items) {
    const source = ownerReference(index, item.source_ref_id, label(row));
    if (item.selected_digest !== undefined && (item.selection === "included" || item.selection === "reference_only")) {
      equal(item.selected_digest, source.digest, `${label(row)} selected source digest`);
    }
  }
}

function turn(index: GraphIndex, row: TurnContext): void {
  const trial = exactTrial(index, row.trial, label(row));
  const session = resolve(index, "agent_session", row.agent_session.agent_session_id, label(row), row.agent_session.revision);
  const context = exactContext(index, row.context, label(row));
  equal(session.run_id, trial.run_id, `${label(row)} Session Run`);
  equal(context.trial, row.trial, `${label(row)} Context Trial`);
  if (session.active_trial !== undefined) {
    equal(session.active_trial, row.trial, `${label(row)} Session Trial`);
  }
  if (session.active_context !== undefined) {
    equal(session.active_context, row.context, `${label(row)} Session Context`);
  }
  equal(row.working_program_id, trial.working_program_id, `${label(row)} working Program`);
  equal(row.trial_feedback_cursor, trial.trial_feedback_cursor, `${label(row)} feedback cursor`);
  equal(row.proposal_revision, trial.proposal_revision, `${label(row)} proposal`);
  equal(row.external_snapshot, trial.external_snapshot, `${label(row)} external snapshot`);
  equal(row.policy_digest, trial.policy_digest, `${label(row)} policy`);
  equal([row.input_ref_id, row.input_digest], [context.input_ref_id, context.input_digest], `${label(row)} input`);
  ownerReference(index, row.input_ref_id, label(row), row.input_digest);
}

function prune(index: GraphIndex, row: PrunePlan): void {
  const trial = exactTrial(index, row.trial, label(row));
  const context = exactContext(index, row.context, label(row));
  const branch = resolve(index, "branch", trial.branch_id, label(row), row.expected_branch_revision);
  equal(context.trial, row.trial, `${label(row)} Context Trial`);
  equal(branch.context_revision, row.expected_context_revision, `${label(row)} expected Branch context_revision`);
  equal(branch.lease_id, row.lease_id, `${label(row)} Branch lease`);
  requireCoherence(index.leaseOwners.has(leaseKey(row.lease_id, "branch", branch.branch_id, row.lease_epoch)), label(row), "missing matching Branch lease owner/epoch");
  equal(row.reservation_id, trial.reservation_id, `${label(row)} Trial reservation`);
  equal(row.input_digest, context.input_digest, `${label(row)} proposed input_digest`);
  requireCoherence(row.requested_mode === row.resolved_mode || row.fallback_reason !== undefined, label(row), "mode fallback requires fallback_reason");
}

function receiptReference(index: GraphIndex, reference: ReceiptReference, runId: string, context: string) {
  const receipt = index.receipts.get(revisionKey(reference.evaluation_id, reference.receipt_revision));
  requireCoherence(receipt !== undefined, context, `missing receipt ${revisionKey(reference.evaluation_id, reference.receipt_revision)}`);
  equal(receipt.owner.run_id, runId, `${context} receipt Run`);
  return receipt;
}

function fact(index: GraphIndex, row: Fact): void {
  const owner = run(index, row.run_id, label(row));
  equal(row.scope.task_contract_hash, owner.task_contract_hash, `${label(row)} scope TaskContract`);
  for (const id of row.scope.program_ids) {
    programForRun(index, id, row.run_id, label(row));
  }
  for (const id of row.evidence_ref_ids) {
    ownerReference(index, id, label(row));
  }
  for (const reference of row.source_revisions) {
    receiptReference(index, reference, row.run_id, label(row));
  }
  if (row.supersedes_fact_id !== undefined) {
    requireCoherence(row.supersedes_fact_id !== row.fact_id, label(row), "Fact cannot supersede itself");
    sameRun(index, "fact", row.supersedes_fact_id, row.run_id, label(row));
  }
}

function memory(index: GraphIndex, row: TaskMemoryRevision): void {
  run(index, row.run_id, label(row));
  for (const id of row.fact_ids) {
    sameRun(index, "fact", id, row.run_id, label(row));
  }
  for (const id of row.source_ref_ids) {
    ownerReference(index, id, label(row));
  }
  for (const reference of row.source_revisions) {
    receiptReference(index, reference, row.run_id, label(row));
  }
  if (row.supersedes_revision !== undefined) {
    requireCoherence(row.supersedes_revision < row.revision, label(row), "memory supersedes must point backwards");
    const previous = resolve(index, "task_memory_revision", row.memory_id, label(row), row.supersedes_revision);
    equal(previous.run_id, row.run_id, `${label(row)} superseded memory Run`);
    requireCoherence(previous.memory_epoch <= row.memory_epoch && previous.event_cursor <= row.event_cursor, label(row), "memory epoch/event watermark moved backwards");
  }
}

function replay(index: GraphIndex, row: ReplayJob): void {
  const owner = run(index, row.run_id, label(row));
  equal(row.version_lock_digest, owner.version_lock_digest, `${label(row)} VersionLock`);
  lockFor(index, row.version_lock_digest, label(row));
  externalSnapshot(index, row.input_snapshot, row.run_id, label(row));
  ownerReference(index, row.input_ref_id, label(row), row.input_digest);
  for (const id of row.dependency_ref_ids) {
    ownerReference(index, id, label(row));
  }
  for (const id of row.proposed_fact_ids) {
    sameRun(index, "fact", id, row.run_id, label(row));
  }
  if (row.proposed_memory !== undefined) {
    const memory = resolve(index, "task_memory_revision", row.proposed_memory.memory_id, label(row), row.proposed_memory.revision);
    equal(memory.run_id, row.run_id, `${label(row)} proposed memory Run`);
  }
  const budget = resolve(index, "reservation", row.reservation_id, label(row));
  equal([budget.owner_kind, budget.owner_id], ["replay_job", row.replay_job_id], `${label(row)} reservation owner`);
}

function reservation(index: GraphIndex, row: Reservation): void {
  const owner = resolve(index, row.owner_kind, row.owner_id, label(row));
  equal(owner.reservation_id, row.reservation_id, `${label(row)} owner reservation`);
  const runId = owner.run_id;
  const root = run(index, runId, label(row));
  if (row.owner_kind === "task_run") {
    requireCoherence(row.parent_reservation_id === undefined, label(row), "Run reservation cannot have a parent");
  } else {
    equal(row.parent_reservation_id, root.reservation_id, `${label(row)} parent Run reservation`);
    const parent = resolve(index, "reservation", row.parent_reservation_id!, label(row));
    equal([parent.owner_kind, parent.owner_id], ["task_run", runId], `${label(row)} parent scope`);
  }
  const operations = new Set(row.operation_ids);
  for (const usage of row.usage_revisions) {
    requireCoherence(operations.has(usage.operation_id), label(row), `usage operation ${JSON.stringify(usage.operation_id)} absent from reservation snapshot`);
    ownerReference(index, usage.receipt_ref_id, `${label(row)} usage ${revisionKey(usage.operation_id, usage.usage_revision)}`);
  }
}

function lease(index: GraphIndex, row: Lease): void {
  resolve(index, row.owner_kind, row.owner_id, label(row));
  // Resource IDs are opaque owner identities, not aliases for LabObject IDs.
  requireCoherence(row.allocation_state !== "released" || row.stop_confirmed, label(row), `released resource ${JSON.stringify(row.resource_id)} lacks stop confirmation`);
}

function rewards(index: GraphIndex): void {
  for (const { value, owner } of index.rewards.values()) {
    for (const reference of value.receipt_keys) {
      const receipt = receiptReference(index, reference, owner.run_id, `${label(owner)} reward ${value.reward_revision}`);
      equal(receipt.owner.trial_id, owner.trial_id, `${label(owner)} reward receipt Trial`);
    }
    for (const key of value.usage_keys) {
      const usage = index.usages.get(revisionKey(key.operation_id, key.usage_revision));
      requireCoherence(usage !== undefined, label(owner), `missing reward usage ${revisionKey(key.operation_id, key.usage_revision)}`);
      equal(usage.owner.reservation_id, owner.reservation_id, `${label(owner)} reward usage reservation`);
    }
  }
}

export function validateReferences(index: GraphIndex): void {
  for (const row of index.objects) {
    switch (row.kind) {
      case "task_run":
        taskRun(index, row);
        break;
      case "proposal_revision":
        run(index, row.run_id, label(row));
        ownerReference(index, row.parameter_space_ref_id, label(row));
        break;
      case "trial":
        trial(index, row);
        break;
      case "program":
        program(index, row);
        break;
      case "evaluation":
        evaluationPlan(index, row);
        replicate(index, row);
        evaluationReceipts(index, row);
        break;
      case "branch":
        branch(index, row);
        break;
      case "agent_session":
        session(index, row);
        break;
      case "capsule":
        capsule(index, row);
        break;
      case "context_manifest":
        manifest(index, row);
        break;
      case "turn_context":
        turn(index, row);
        break;
      case "prune_plan":
        prune(index, row);
        break;
      case "fact":
        fact(index, row);
        break;
      case "task_memory_revision":
        memory(index, row);
        break;
      case "replay_job":
        replay(index, row);
        break;
      case "reservation":
        reservation(index, row);
        break;
      case "lease":
        lease(index, row);
        break;
    }
  }
  rewards(index);
}
