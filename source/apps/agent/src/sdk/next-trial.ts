import { isAbsolute } from "node:path";
import type { Program } from "../generated/objects.ts";
import type { PrepareTrialRequest, RuntimeState } from "../generated/runtime.ts";
import { managedSearchState } from "../run/managed-search.ts";
import { stageRouter } from "../run/router.ts";
import type { SearchProposal } from "../run/router.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";
import { digestBytes, loadLaunchManifest } from "../tasks/launch-manifest.ts";
import type { LoadedManifest } from "../tasks/launch-manifest.ts";
import { validateTrialRequest } from "../tasks/trial-request.ts";
import type { GoalContinuationResult, GoalManagedSession, GoalNextStop, GoalTurnRequest } from "./goal.ts";

export interface StageRouterNextOptions {
  readonly manifest: string;
  readonly intentId: string;
  readonly trialCreditLimit: number;
}

export type StageRouterNext = (
  previous: GoalContinuationResult,
  index: number,
) => Promise<GoalTurnRequest | GoalNextStop>;

function validateOptions(options: StageRouterNextOptions): void {
  if (!isAbsolute(options.manifest)) throw new Error("Next-Trial manifest must be an absolute path");
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(options.intentId)) {
    throw new Error("Next-Trial planner requires a stable intentId");
  }
  if (!Number.isFinite(options.trialCreditLimit) || options.trialCreditLimit <= 0) {
    throw new Error("Next-Trial credit limit must be positive and finite");
  }
}

function stopRecommendation(proposal: SearchProposal): GoalNextStop {
  const reason = `${proposal.action}: ${proposal.reason}`;
  const bounded = reason.length <= 512 ? reason :
    `${Array.from(reason).slice(0, 190).join("")} [${digestBytes(reason)}]`;
  return { stop: { reason: bounded } };
}

function retainedParent(state: RuntimeState, proposal: SearchProposal): Program {
  const candidate = proposal.candidate;
  if (proposal.action !== "establish_incumbent" && candidate === undefined) {
    throw new Error(`Stage router ${proposal.action} requires a retained candidate`);
  }
  const parentId = candidate?.program_id ?? state.trial.working_program_id;
  const parent = state.graph.objects.find((object): object is Program =>
    object.kind === "program" && object.program_id === parentId);
  if (parent === undefined || (candidate !== undefined && parent.artifact_digest !== candidate.artifact_digest)) {
    throw new Error(`Stage router parent ${JSON.stringify(parentId)} is not the exact retained Program`);
  }
  return parent;
}

/** Build content only; the original owners still admit and execute the request. O(G). */
export function planNextTrial(
  state: RuntimeState,
  loaded: LoadedManifest,
  options: StageRouterNextOptions,
  index: number,
  proposal: SearchProposal = stageRouter.propose(managedSearchState(state)),
): GoalTurnRequest | GoalNextStop {
  validateOptions(options);
  if (!Number.isSafeInteger(index) || index < 1 || index > 99) {
    throw new Error("Next-Trial index must be 1..99");
  }
  if (state.manifest_digest !== loaded.digest || state.stamp.run_id !== loaded.manifest.run_id ||
      state.stamp.account_id !== loaded.manifest.account_id) {
    throw new Error("Next-Trial state differs from the original manifest Run/account/digest");
  }
  if (["reconcile", "stop", "wait"].includes(proposal.action)) return stopRecommendation(proposal);
  return nextRequest(state, loaded, options, index, proposal);
}

function nextRequest(
  state: RuntimeState,
  loaded: LoadedManifest,
  options: StageRouterNextOptions,
  index: number,
  proposal: SearchProposal,
): GoalTurnRequest | GoalNextStop {
  const parent = retainedParent(state, proposal);
  const trialCreditLimit = Math.min(options.trialCreditLimit, loaded.manifest.run_credit_limit,
    state.account_budget.limit - state.account_budget.spent - state.account_budget.reserved,
    state.run_budget.limit - state.run_budget.spent - state.run_budget.reserved);
  if (trialCreditLimit <= 0) {
    return { stop: { reason: "stop: Account or Run credit headroom is exhausted" } };
  }
  const identity = digestBytes(canonicalKey({ intentId: options.intentId, index, manifest: loaded.digest,
    expected: state.stamp, parent: parent.program_id, proposal, trialCreditLimit })).slice(7);
  const request: PrepareTrialRequest = validateTrialRequest({
    schema_version: "kernelcat.prepare-trial/v1",
    intent_id: `pursuit-${identity}`,
    manifest_digest: loaded.digest,
    expected: structuredClone(state.stamp),
    trial_id: `trial-${identity}`,
    branch_id: `branch-${identity}`,
    session_id: `session-${identity}`,
    parent_program_id: parent.program_id,
    proposal: { ...loaded.manifest.proposal, proposal_id: `proposal-${identity}` },
    trial_credit_limit: trialCreditLimit,
  }, loaded);
  return { intentId: request.intent_id, run: { nextTrial: request } };
}

/** The production adapter uses only retained observations, including unknown time. */
export function createStageRouterNext(
  session: GoalManagedSession,
  options: StageRouterNextOptions,
): StageRouterNext {
  validateOptions(options);
  const selected = Object.freeze({ ...options });
  return async (_previous, index) => {
    const loaded = await loadLaunchManifest(selected.manifest);
    const state = await session.state();
    return planNextTrial(state, loaded, selected, index);
  };
}
