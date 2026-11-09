import type { Baseline, Identifier, UnknownObservation } from "../generated/task-contract.ts";
import type { ConditionObservation } from "../generated/control.ts";
import { compareText } from "./scoring/canonical.ts";

/** Observations are supplied by their owners; this policy cannot qualify a candidate. */
export interface SearchCandidate {
  readonly candidate: Baseline;
  readonly validated: ConditionObservation;
  readonly improvement: ConditionObservation;
  readonly confirmed: ConditionObservation;
}

export interface SearchState {
  readonly blockingUnknowns: readonly Identifier[];
  readonly incumbent: Baseline | null;
  readonly candidates: readonly SearchCandidate[];
  readonly stopRequested: boolean;
  readonly timeAvailable: ConditionObservation;
  readonly validationBudgetAvailable: ConditionObservation;
}

export interface SearchProposal {
  readonly kind: "search_proposal";
  readonly policyVersion: "kernelcat.stage-router/v1";
  readonly action: "reconcile" | "establish_incumbent" | "confirm" | "explore" | "stop" | "wait";
  readonly reason: string;
  readonly candidate?: Baseline;
  readonly operationId?: Identifier;
}

export interface SearchPolicy {
  propose(state: SearchState): SearchProposal;
}

function proposal(action: SearchProposal["action"], reason: string, candidate?: Baseline): SearchProposal {
  return {
    kind: "search_proposal", policyVersion: "kernelcat.stage-router/v1", action, reason,
    ...(candidate === undefined ? {} : { candidate: structuredClone(candidate) }),
  };
}

/**
 * A reached limit and an unobserved limit are different facts. Evaluating every
 * limit before answering keeps an unknown from masking a definite stop, and keeps
 * the unknown itself from being promoted to `true`.
 */
function limitProposal(
  limits: ReadonlyArray<readonly [ConditionObservation, string]>,
): SearchProposal | null {
  let firstUnknown: SearchProposal | null = null;
  for (const [value, name] of limits) {
    if (value === false) {
      return proposal("stop", `${name} limit reached`);
    }
    if (value !== true && firstUnknown === null) {
      const reason = (value as UnknownObservation).reason;
      firstUnknown = proposal("wait", `${name} is unknown: ${reason}`);
    }
  }
  return firstUnknown;
}

/** Fixed priorities and ordinal baseline IDs provide deterministic tie-breaks. O(C log C + U log U). */
export function proposeSearch(state: SearchState): SearchProposal {
  if (state.blockingUnknowns.length > 0) {
    const operationId = [...state.blockingUnknowns].sort(compareText)[0];
    return { ...proposal("reconcile", "Unresolved effect blocks new work"), operationId };
  }
  if (state.stopRequested) return proposal("stop", "Protocol or operator requested stop");
  const limitStop = limitProposal([
    [state.timeAvailable, "Time"],
    [state.validationBudgetAvailable, "Validation budget"],
  ]);
  if (limitStop) return limitStop;
  if (state.incumbent === null) return proposal("establish_incumbent", "No qualified incumbent is available");
  const candidates = [...state.candidates].sort((left, right) =>
    compareText(left.candidate.program_id, right.candidate.program_id) ||
    compareText(left.candidate.artifact_digest, right.candidate.artifact_digest));
  for (const item of candidates) {
    if (item.validated === true && item.improvement === true && item.confirmed !== true) {
      return proposal("confirm", "Apparent improvement requires independent confirmation", item.candidate);
    }
  }
  return proposal("explore", "Qualified incumbent and validation budget permit the next proposal", state.incumbent);
}

export const stageRouter: SearchPolicy = Object.freeze({ propose: proposeSearch });
