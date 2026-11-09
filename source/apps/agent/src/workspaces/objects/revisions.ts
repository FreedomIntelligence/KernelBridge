import type {
  AgentSession, Branch, Evaluation, LabObject, ReplayJob, Reservation, TaskMemoryRevision, Trial,
} from "../../generated/objects.ts";
import {
  equal, label, requireCoherence, revisionKey,
} from "./graph-index.ts";
import type { GraphIndex, RevisionEntry } from "./graph-index.ts";

function fixedFields<T extends LabObject>(rows: readonly T[], fields: readonly (keyof T)[]): void {
  for (const row of rows.slice(1)) {
    for (const field of fields) {
      equal(row[field], rows[0][field], `${label(row)} fixed ${String(field)}`);
    }
  }
}

function appendOnly<T, O extends LabObject>(
  rows: readonly O[], entries: (row: O) => readonly T[], key: (item: T) => string,
  domain: string,
): void {
  let previous = new Set<string>();
  for (const row of rows) {
    const current = new Set(entries(row).map(key));
    for (const identity of previous) {
      requireCoherence(current.has(identity), label(row), `${domain} ${identity} disappeared from appended snapshot`);
    }
    previous = current;
  }
}

function providerHistory(rows: readonly AgentSession[]): void {
  for (const field of ["provider_thread_id", "provider_root_id"] as const) {
    let observed: string | undefined;
    for (const row of rows) {
      const current = row[field];
      if (observed !== undefined) {
        requireCoherence(current.state === "known" && current.value === observed, `${label(row)} ${field}`, "observed provider identity changed or disappeared");
      }
      if (current.state === "known") {
        observed = current.value;
      }
    }
  }
}

function supersedes<T extends { readonly supersedes_revision?: number }, O>(
  entries: ReadonlyMap<string, RevisionEntry<T, O>>,
  identity: (entry: RevisionEntry<T, O>) => readonly [string, number], domain: string,
): void {
  for (const entry of entries.values()) {
    const [id, revision] = identity(entry);
    const previous = entry.value.supersedes_revision;
    if (previous === undefined) {
      continue;
    }
    requireCoherence(previous < revision, `${domain} ${revisionKey(id, revision)}`, "supersedes must point backwards in its own revision domain");
    requireCoherence(entries.has(revisionKey(id, previous)), `${domain} ${revisionKey(id, revision)}`, `missing superseded ${domain} revision ${previous}`);
  }
}

function trialHistory(index: GraphIndex): void {
  for (const rows of index.histories.get("trial")?.values() ?? []) {
    const trials = rows as Trial[];
    fixedFields(trials, ["run_id", "branch_id", "parent_program_id", "proposal_revision", "task_contract_hash", "version_lock_digest", "external_snapshot", "policy_digest", "model_route", "reservation_id"]);
    for (let i = 1; i < trials.length; i++) {
      requireCoherence(trials[i].trial_feedback_cursor >= trials[i - 1].trial_feedback_cursor, label(trials[i]), "trial_feedback_cursor moved backwards");
    }
    appendOnly(trials, (row) => row.reward_revisions, (item) => String(item.reward_revision), "reward");
  }
}

function otherHistories(index: GraphIndex): void {
  for (const ids of index.histories.values()) {
    for (const rows of ids.values()) {
      const row = rows[0];
      if (row.kind === "evaluation") {
        const evaluations = rows as Evaluation[];
        fixedFields(evaluations, ["run_id", "trial_id", "program_id", "artifact_digest", "plan"]);
        appendOnly(evaluations, (item) => item.receipts, (item) => String(item.receipt_revision), "receipt");
      } else if (row.kind === "branch") {
        fixedFields(rows as Branch[], ["run_id", "initial_parent_program_id"]);
      } else if (row.kind === "agent_session") {
        const sessions = rows as AgentSession[];
        fixedFields(sessions, ["run_id"]);
        providerHistory(sessions);
      } else if (row.kind === "reservation") {
        const reservations = rows as Reservation[];
        fixedFields(reservations, ["owner_kind", "owner_id", "parent_reservation_id"]);
        appendOnly(reservations, (item) => item.operation_ids, (item) => item, "operation");
        appendOnly(reservations, (item) => item.usage_revisions, (item) => revisionKey(item.operation_id, item.usage_revision), "usage");
      } else if (row.kind === "task_memory_revision") {
        fixedFields(rows as TaskMemoryRevision[], ["run_id"]);
      } else if (row.kind === "replay_job") {
        fixedFields(rows as ReplayJob[], ["run_id", "version_lock_digest", "input_snapshot", "input_ref_id", "input_digest", "dependency_ref_ids", "reservation_id", "replay_rules_version"]);
      }
    }
  }
}

export function validateRevisions(index: GraphIndex): void {
  trialHistory(index);
  otherHistories(index);
  supersedes(index.receipts, ({ value, owner }) => [owner.evaluation_id, value.receipt_revision], "receipt");
  supersedes(index.usages, ({ value }) => [value.operation_id, value.usage_revision], "usage");
  supersedes(index.rewards, ({ value, owner }) => [owner.trial_id, value.reward_revision], "reward");
}
