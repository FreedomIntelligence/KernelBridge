import type { ConditionObservation } from "../generated/control.ts";
import type { LabObject, Program } from "../generated/objects.ts";
import type { PrepareTrialRequest, RuntimeState } from "../generated/runtime.ts";
import type { Baseline, Identifier, UnknownObservation } from "../generated/task-contract.ts";
import { compareText } from "./scoring/canonical.ts";
import { stageRouter } from "./router.ts";
import type { SearchCandidate, SearchProposal, SearchState } from "./router.ts";

/**
 * Policy dimensions this Run cannot observe from retained authority. They stay
 * `unknown` instead of being promoted to a usable observation, and an unknown
 * alone never refuses an explicit operator Trial. See MANAGED_SEARCH.md.
 */
export const UNOBSERVED_WALL_CLOCK =
  "No retained elapsed or remaining time observation exists; " +
  "TaskContract budgets are limits with no counterpart counter";

export const UNOBSERVED_QUALIFICATION =
  "No Evaluation row is retained, so no candidate is validated, improved or confirmed";

function unknown(reason: string): UnknownObservation {
  return { status: "unknown", reason };
}

export interface AdmittedTrial {
  readonly decision: "admit";
  readonly proposal: SearchProposal;
  readonly unobserved: ReadonlyArray<string>;
}

export interface RefusedTrial {
  readonly decision: "refuse";
  readonly proposal: SearchProposal;
  readonly reason: string;
}

/** An admitted proposal never grants budget, parent selection or correctness. */
export type NextTrialAdmission = AdmittedTrial | RefusedTrial;

/**
 * Credit headroom is exact arithmetic over the owner's own ledger facts, so it is
 * never unknown here. Credit reserved by started or UNKNOWN work is unavailable
 * and must count against the ceiling exactly as spent credit does.
 */
function hasHeadroom(limit: number, spent: number, reserved: number): boolean {
  return limit - spent - reserved > 0;
}

/**
 * Credits are the single ledger currency; there is no separate validation or
 * evaluation budget domain. Both ceilings bind the next Trial, so exhausting
 * either one is a definite stop.
 */
function validationBudgetAvailable(state: RuntimeState): ConditionObservation {
  const run = state.run_budget;
  const account = state.account_budget;
  return hasHeadroom(run.limit, run.spent, run.reserved) &&
    hasHeadroom(account.limit, account.spent, account.reserved);
}

/**
 * `reconciling` is the owner's quarantine for an effect that started or carries
 * an outcome. It can hold while no effect ID is projected, so it contributes the
 * Run's own identity rather than being silently dropped.
 */
function blockingUnknowns(state: RuntimeState): ReadonlyArray<Identifier> {
  const pending = [...state.pending_effect_ids];
  if (pending.length === 0 && state.stamp.run_status === "reconciling") {
    pending.push(state.stamp.run_id);
  }
  return pending.sort(compareText);
}

/**
 * `run_status === "stopped"` is NOT an operator stop: preparing any next Trial
 * raises the owner's own trial control event, which stops the previous Session
 * through the same field. Only a retained StopRunRequest distinguishes the two,
 * and the owner itself refuses to prepare a Trial on a stopped Run.
 */
function stopRequested(state: RuntimeState): boolean {
  const control = state.last_control_request;
  return control !== undefined && "run_id" in control;
}

function isProgram(object: LabObject): object is Program {
  return object.kind === "program";
}

/** A Baseline is a Program's identity projection; the extra lineage fields drop. */
function baselineOf(program: Program): Baseline {
  return { program_id: program.program_id, artifact_digest: program.artifact_digest };
}

/**
 * Candidate identities are retained in the append-only graph, but no verdict is:
 * nothing in this runtime writes an Evaluation row. Each verdict is therefore
 * supplied as an explicit unknown, which the policy must not promote.
 */
function retainedCandidates(state: RuntimeState): ReadonlyArray<SearchCandidate> {
  const programs = state.graph.objects
    .filter(isProgram)
    .filter((program) => program.run_id === state.run.run_id && program.candidate_role === "candidate");
  const candidates = programs.map((program): SearchCandidate => ({
    candidate: baselineOf(program),
    validated: unknown(UNOBSERVED_QUALIFICATION),
    improvement: unknown(UNOBSERVED_QUALIFICATION),
    confirmed: unknown(UNOBSERVED_QUALIFICATION),
  }));
  return candidates.sort((left, right) => compareText(left.candidate.program_id, right.candidate.program_id));
}

/**
 * Derive the policy input from retained authority only. Nothing here reads a
 * clock, a provider or a filesystem; every field traces to the owner's stamped
 * state or its immutable graph.
 */
export function managedSearchState(state: RuntimeState): SearchState {
  return {
    blockingUnknowns: blockingUnknowns(state),
    // Qualification belongs to the evaluator, which this managed path does not
    // activate. A retained parent Program is a lineage fact, not an incumbent.
    incumbent: null,
    candidates: retainedCandidates(state),
    stopRequested: stopRequested(state),
    timeAvailable: unknown(UNOBSERVED_WALL_CLOCK),
    validationBudgetAvailable: validationBudgetAvailable(state),
  };
}

function refusalMessage(proposal: SearchProposal, request: PrepareTrialRequest): string {
  const operation = proposal.operationId === undefined ? "" : ` (operation ${JSON.stringify(proposal.operationId)})`;
  return `Stage router refuses Trial ${JSON.stringify(request.trial_id)}` +
    `${operation}: ${proposal.action} — ${proposal.reason}`;
}

/**
 * Route one managed Trial preparation through the existing stage policy.
 *
 * A refusal grounded in a definite observation stops the preparation before any
 * Trial object, reservation or model effect is created. A `wait` is grounded only
 * in a dimension this Run cannot observe; it is reported as a capability limit
 * and does not override an explicit operator request. The policy never rewrites
 * the operator's parent Program, budget or actor, and the owner remains the
 * authority that refuses a stopped Run or an unresolved effect.
 */
export function admitNextTrial(
  state: RuntimeState,
  request: PrepareTrialRequest,
): NextTrialAdmission {
  const proposal = stageRouter.propose(managedSearchState(state));
  if (proposal.action === "reconcile" || proposal.action === "stop") {
    return { decision: "refuse", proposal, reason: refusalMessage(proposal, request) };
  }
  return {
    decision: "admit",
    proposal,
    unobserved: [UNOBSERVED_WALL_CLOCK, UNOBSERVED_QUALIFICATION],
  };
}
