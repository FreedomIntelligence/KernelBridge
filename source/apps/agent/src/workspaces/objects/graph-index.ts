import type {
  Evaluation, LabObject, ObjectGraph, OwnerReference, Program, ReceiptRevision, Reservation,
  RewardRevision, Trial, UsageRevision, VersionLockEntry,
} from "../../generated/objects.ts";
import type { TaskContractSource } from "../../generated/task-contract.ts";
import { canonicalKey } from "../../run/scoring/canonical.ts";

export type Kind = LabObject["kind"];
export type ObjectOf<K extends Kind> = Extract<LabObject, { readonly kind: K }>;
type Histories = Map<Kind, Map<string, LabObject[]>>;
export interface RevisionEntry<T, O> {
  readonly value: T;
  readonly owner: O;
}

export interface GraphIndex {
  readonly objects: readonly LabObject[];
  readonly histories: Histories;
  readonly exact: ReadonlyMap<string, LabObject>;
  readonly references: ReadonlyMap<string, OwnerReference>;
  readonly contracts: ReadonlyMap<string, TaskContractSource>;
  readonly locks: ReadonlyMap<string, VersionLockEntry>;
  readonly operations: ReadonlyMap<string, string>;
  readonly receipts: ReadonlyMap<string, RevisionEntry<ReceiptRevision, Evaluation>>;
  readonly usages: ReadonlyMap<string, RevisionEntry<UsageRevision, Reservation>>;
  readonly rewards: ReadonlyMap<string, RevisionEntry<RewardRevision, Trial>>;
  readonly leaseOwners: ReadonlySet<string>;
}

export function requireCoherence(condition: boolean, context: string, reason: string): asserts condition {
  if (!condition) {
    throw new Error(`Incoherent ObjectGraph ${context}: ${reason}`);
  }
}

export function equal(actual: unknown, expected: unknown, context: string): void {
  requireCoherence(canonicalKey(actual) === canonicalKey(expected), context, "does not match supplied identity/baseline");
}

// JSON tuple encoding is injective even when wire identifiers contain separators.
export function revisionKey(id: string, revision: number): string {
  return JSON.stringify([id, revision]);
}

function snapshotKey(kind: Kind, id: string, revision: number): string {
  return JSON.stringify([kind, id, revision]);
}

export function leaseKey(id: string, ownerKind: string, ownerId: string, epoch?: number): string {
  return JSON.stringify([id, ownerKind, ownerId, epoch ?? null]);
}

export function objectId(row: LabObject): string {
  switch (row.kind) {
    case "task_run":
      return row.run_id;
    case "proposal_revision":
      return row.proposal_id;
    case "trial":
      return row.trial_id;
    case "program":
      return row.program_id;
    case "evaluation":
      return row.evaluation_id;
    case "branch":
      return row.branch_id;
    case "agent_session":
      return row.agent_session_id;
    case "capsule":
      return row.capsule_id;
    case "turn_context":
      return row.turn_context_id;
    case "context_manifest":
      return row.context_id;
    case "prune_plan":
      return row.plan_id;
    case "fact":
      return row.fact_id;
    case "task_memory_revision":
      return row.memory_id;
    case "replay_job":
      return row.replay_job_id;
    case "lease":
      return row.lease_id;
    case "reservation":
      return row.reservation_id;
  }
}

export function label(row: LabObject): string {
  return `${row.kind} ${JSON.stringify(objectId(row))}${"revision" in row ? ` revision ${row.revision}` : ""}`;
}

export function history<K extends Kind>(index: GraphIndex, kind: K, id: string): readonly ObjectOf<K>[] {
  return (index.histories.get(kind)?.get(id) ?? []) as ObjectOf<K>[];
}

export function resolve<K extends Kind>(
  index: GraphIndex, kind: K, id: string, context: string, revision?: number,
): ObjectOf<K> {
  const rows = history(index, kind, id);
  const row = revision === undefined ? rows[0] : index.exact.get(snapshotKey(kind, id, revision)) as ObjectOf<K> | undefined;
  requireCoherence(row !== undefined, context, `missing ${kind} ${JSON.stringify(id)}${revision === undefined ? "" : ` revision ${revision}`}`);
  return row;
}

/** Resolve ownership without reattributing an explicitly pinned reusable root. */
export function programForRun(index: GraphIndex, id: string, runId: string, context: string): Program {
  const program = resolve(index, "program", id, context);
  if (program.run_id === runId) {
    return program;
  }
  const owner = resolve(index, "task_run", runId, context);
  requireCoherence(
    program.candidate_role === "root", context,
    `foreign Program ${JSON.stringify(id)} is not a root for Run ${JSON.stringify(runId)}`,
  );
  requireCoherence(
    owner.root_program_id === id, context,
    `foreign root ${JSON.stringify(id)} is not explicitly pinned by Run ${JSON.stringify(runId)}`,
  );
  const origin = resolve(index, "task_run", program.run_id, `${context} root origin`);
  equal(origin.root_program_id, id, `${context} originating Run root pin`);
  equal(
    origin.task_contract_hash, owner.task_contract_hash,
    `${context} root TaskContract source for Run ${JSON.stringify(runId)}`,
  );
  return program;
}

export function ofKind<K extends Kind>(index: GraphIndex, kind: K): readonly ObjectOf<K>[] {
  return [...(index.histories.get(kind)?.values() ?? [])].flat() as ObjectOf<K>[];
}

function insert<T>(target: Map<string, T>, key: string, value: T, context: string): void {
  if (target.has(key)) {
    equal(value, target.get(key), `${context} conflicting duplicate ${key}`);
  } else {
    target.set(key, value);
  }
}

function keyed<T>(rows: readonly T[], key: (row: T) => string, context: string): Map<string, T> {
  const result = new Map<string, T>();
  for (const row of rows) {
    insert(result, key(row), row, context);
  }
  return result;
}

function objectHistories(rows: readonly LabObject[]): Histories {
  const kinds: Histories = new Map();
  const bodies = new Map<string, LabObject>();
  for (const row of rows) {
    const id = objectId(row);
    const key = JSON.stringify([row.kind, id, "revision" in row ? row.revision : null]);
    if (bodies.has(key)) {
      equal(row, bodies.get(key), `${label(row)} conflicting duplicate`);
      continue;
    }
    bodies.set(key, row);
    const identities = kinds.get(row.kind) ?? new Map<string, LabObject[]>();
    const revisions = identities.get(id) ?? [];
    revisions.push(row);
    identities.set(id, revisions);
    kinds.set(row.kind, identities);
  }
  for (const identities of kinds.values()) {
    for (const rows of identities.values()) {
      rows.sort((a, b) => ("revision" in a ? a.revision : 0) - ("revision" in b ? b.revision : 0));
    }
  }
  return kinds;
}

function nestedRevisions<T, O>(
  rows: readonly O[], entries: (row: O) => readonly T[], identity: (row: O, item: T) => string,
  context: string,
): Map<string, RevisionEntry<T, O>> {
  const result = new Map<string, RevisionEntry<T, O>>();
  for (const row of rows) {
    for (const item of entries(row)) {
      const key = identity(row, item);
      const previous = result.get(key);
      if (previous) {
        equal(item, previous.value, `${context} conflicting duplicate ${key}`);
      } else {
        result.set(key, { value: item, owner: row });
      }
    }
  }
  return result;
}

export function indexGraph(graph: ObjectGraph): GraphIndex {
  const histories = objectHistories(graph.objects);
  const objects = [...histories.values()].flatMap((ids) => [...ids.values()].flat());
  const evaluations = objects.filter((row): row is Evaluation => row.kind === "evaluation");
  const reservations = objects.filter((row): row is Reservation => row.kind === "reservation");
  const trials = objects.filter((row): row is Trial => row.kind === "trial");
  const operations = new Map<string, string>();
  const leaseOwners = new Set<string>();
  for (const row of objects) {
    if (row.kind !== "lease") {
      continue;
    }
    leaseOwners.add(leaseKey(row.lease_id, row.owner_kind, row.owner_id));
    leaseOwners.add(leaseKey(row.lease_id, row.owner_kind, row.owner_id, row.epoch));
  }
  for (const row of reservations) {
    for (const operation of row.operation_ids) {
      const owner = operations.get(operation);
      requireCoherence(owner === undefined || owner === row.reservation_id, label(row), `operation ${JSON.stringify(operation)} belongs to another reservation ${owner}`);
      operations.set(operation, row.reservation_id);
    }
  }
  return {
    objects, histories, operations, leaseOwners,
    exact: new Map(objects.flatMap((row) => "revision" in row
      ? [[snapshotKey(row.kind, objectId(row), row.revision), row] as const] : [])),
    references: keyed(graph.owner_references, (row) => row.ref_id, "owner reference"),
    contracts: keyed(graph.task_contract_sources, (row) => row.source_digest, "TaskContract source"),
    locks: keyed(graph.version_locks, (row) => row.version_lock_digest, "VersionLock"),
    receipts: nestedRevisions(evaluations, (row) => row.receipts, (row, item) => revisionKey(row.evaluation_id, item.receipt_revision), "receipt"),
    usages: nestedRevisions(reservations, (row) => row.usage_revisions, (_, item) => revisionKey(item.operation_id, item.usage_revision), "usage"),
    rewards: nestedRevisions(trials, (row) => row.reward_revisions, (row, item) => revisionKey(row.trial_id, item.reward_revision), "reward"),
  };
}
