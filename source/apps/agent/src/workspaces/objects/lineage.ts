import type { Program } from "../../generated/objects.ts";
import { equal, label, ofKind, programForRun, requireCoherence, resolve } from "./graph-index.ts";
import type { GraphIndex } from "./graph-index.ts";

// Iterative Kahn traversal handles schema-bounded long chains without JS recursion.
function programOrder(index: GraphIndex): readonly Program[] {
  const programs = ofKind(index, "program");
  const children = new Map<string, Program[]>();
  const pending = new Map<string, number>();
  const ready: Program[] = [];
  for (const row of programs) {
    pending.set(row.program_id, row.parent_program_ids.length);
    if (row.parent_program_ids.length === 0) {
      ready.push(row);
    }
    for (const id of row.parent_program_ids) {
      programForRun(index, id, row.run_id, `${label(row)} parent`);
      const next = children.get(id) ?? [];
      next.push(row);
      children.set(id, next);
    }
  }
  for (let cursor = 0; cursor < ready.length; cursor++) {
    for (const child of children.get(ready[cursor].program_id) ?? []) {
      const count = pending.get(child.program_id)! - 1;
      pending.set(child.program_id, count);
      if (count === 0) {
        ready.push(child);
      }
    }
  }
  const cyclic = programs.find((row) => pending.get(row.program_id)! > 0);
  requireCoherence(cyclic === undefined, cyclic ? label(cyclic) : "Program lineage", "self/cyclic Program lineage");
  return ready;
}

function candidateAncestry(index: GraphIndex, programs: readonly Program[]): void {
  const reached = new Set<string>();
  for (const row of programs) {
    resolve(index, "task_run", row.run_id, label(row));
    if (row.candidate_role === "root") {
      requireCoherence(row.trial_id === undefined && row.parent_program_ids.length === 0, label(row), "root must have no Trial or parents");
      continue;
    }
    requireCoherence(row.trial_id !== undefined, label(row), "candidate/checkpoint requires Trial attribution");
    const trial = resolve(index, "trial", row.trial_id, label(row));
    equal(row.run_id, trial.run_id, `${label(row)} Trial Run`);
    requireCoherence(row.parent_program_ids.length > 0, label(row), "candidate ancestry does not reach Trial starting parent");
    for (const id of row.parent_program_ids) {
      const parent = resolve(index, "program", id, label(row));
      requireCoherence(id === trial.parent_program_id || (parent.trial_id === row.trial_id && reached.has(id)), label(row), `parent ${JSON.stringify(id)} does not descend from Trial starting parent ${JSON.stringify(trial.parent_program_id)}`);
    }
    reached.add(row.program_id);
  }
}

function reservationParents(index: GraphIndex): void {
  const done = new Set<string>();
  for (const row of ofKind(index, "reservation")) {
    let current = row;
    const path = new Set<string>();
    while (!done.has(current.reservation_id)) {
      requireCoherence(!path.has(current.reservation_id), label(row), "cyclic reservation parent chain");
      path.add(current.reservation_id);
      if (current.parent_reservation_id === undefined) {
        break;
      }
      current = resolve(index, "reservation", current.parent_reservation_id, `${label(row)} parent`);
    }
    for (const id of path) {
      done.add(id);
    }
  }
}

export function validateLineage(index: GraphIndex): void {
  candidateAncestry(index, programOrder(index));
  for (const row of ofKind(index, "trial")) {
    const parent = programForRun(index, row.parent_program_id, row.run_id, `${label(row)} starting parent`);
    const working = resolve(index, "program", row.working_program_id, `${label(row)} working checkpoint`);
    requireCoherence(working.program_id === parent.program_id || working.trial_id === row.trial_id, label(row), `working checkpoint ${JSON.stringify(working.program_id)} is unreachable from this Trial`);
  }
  reservationParents(index);
}
