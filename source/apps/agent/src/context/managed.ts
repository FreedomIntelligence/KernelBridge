import type { ContextSource } from "../generated/context.ts";
import type { HarnessInput } from "../generated/harness.ts";
import type { Capsule, ContextManifest, TurnContext } from "../generated/objects.ts";
import type { ContextCommit, RuntimeState } from "../generated/runtime.ts";
import { runtimeValue } from "../clients/runtime.ts";
import { runtimeId } from "../run/initial-graph.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";
import { digestBytes } from "../tasks/launch-manifest.ts";
import { ContextOverflow } from "./compiler.ts";

function dependencies(state: RuntimeState): readonly string[] {
  return [...new Set(state.required_ref_ids ?? state.references.map((reference) => reference.ref_id))].sort();
}

function pruneVersion(state: RuntimeState) {
  const version = state.graph.version_locks.find((entry) => entry.version_lock_digest === state.run.version_lock_digest);
  if (version === undefined) throw new Error("Runtime has no current Run VersionLock");
  return version.lock.rules.prune_policy;
}

function contextManifest(state: RuntimeState, source: ContextSource, input: string): ContextManifest {
  const next = state.stamp.context_revision + 1;
  const digest = digestBytes(input);
  const limit = source.policy.context_bytes;
  if (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit < 1) {
    throw new Error("Managed input has no valid current context byte limit");
  }
  const bytes = Buffer.byteLength(input, "utf8");
  if (bytes > limit) throw new ContextOverflow("Actual Pi input exceeds context_bytes before model admission");
  return {
    kind: "context_manifest", context_id: runtimeId("context", state.run.run_id, state.trial.trial_id), revision: next,
    trial: { trial_id: state.trial.trial_id, revision: state.trial.revision },
    capsule_id: state.capsule?.capsule_id ?? runtimeId("capsule", state.run.run_id, state.trial.trial_id),
    compiler_version: state.compiler_version,
    prune_policy_version: pruneVersion(state),
    policy_digest: state.trial.policy_digest, dependency_ref_ids: dependencies(state), items: [],
    input_ref_id: runtimeId("input", state.run.run_id, state.trial.trial_id, next, digest),
    input_digest: digest, input_bytes: bytes, limit_bytes: limit,
  };
}

function initialCapsule(state: RuntimeState, manifest: ContextManifest): Capsule {
  return {
    kind: "capsule", capsule_id: manifest.capsule_id, initial_trial: manifest.trial,
    parent_program_id: state.trial.parent_program_id, proposal_revision: state.trial.proposal_revision,
    external_snapshot: state.trial.external_snapshot, compiler_version: state.compiler_version,
    policy_digest: state.trial.policy_digest, input_ref_id: manifest.input_ref_id,
    input_digest: manifest.input_digest, initial_context: { context_id: manifest.context_id, revision: manifest.revision },
  };
}

/** The original TS serialization pins full native input, including prompt/tools. */
export function managedContextCommit(
  state: RuntimeState, source: ContextSource, harness: HarnessInput | undefined,
  allowedActions: readonly string[],
): ContextCommit {
  if (source.runtime === undefined || canonicalKey(source.runtime) !== canonicalKey(state.stamp)) {
    throw new Error("Managed ContextSource runtime stamp conflicts with current authority");
  }
  const binding = state.trial.external_snapshot.knowledge;
  if (canonicalKey(source.knowledge) !== canonicalKey(binding)) {
    throw new Error("Managed ContextSource mutated the protected Trial Knowledge binding");
  }
  if (binding !== undefined && (binding.registration.consumer.account_id !== state.stamp.account_id ||
      !dependencies(state).includes(binding.capture_receipt_ref_id))) {
    throw new Error("Managed context omits its original account/Knowledge receipt dependency");
  }
  const input = JSON.stringify(harness ?? source);
  const manifest = contextManifest(state, source, input);
  const turn: TurnContext = {
    kind: "turn_context", turn_context_id: runtimeId("turn", state.trial.trial_id, manifest.revision, manifest.input_digest),
    trial: manifest.trial, agent_session: { ...state.stamp.session, revision: state.stamp.session.revision + 1 },
    working_program_id: state.trial.working_program_id, trial_feedback_cursor: state.trial.trial_feedback_cursor,
    proposal_revision: state.trial.proposal_revision, external_snapshot: state.trial.external_snapshot,
    context: { context_id: manifest.context_id, revision: manifest.revision },
    policy_digest: state.trial.policy_digest, input_ref_id: manifest.input_ref_id,
    input_digest: manifest.input_digest, allowed_actions: [...new Set(allowedActions)].sort(),
  };
  return runtimeValue("ContextCommit", {
    expected: state.stamp, input_json: input, manifest, turn,
    ...(state.capsule === undefined ? { capsule: initialCapsule(state, manifest) } : {}),
  });
}
