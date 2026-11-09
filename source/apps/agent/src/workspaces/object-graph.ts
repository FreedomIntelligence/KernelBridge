import type { ObjectGraph, OwnerReference, TrialKnowledgeBinding } from "../generated/objects.ts";
import type { AccountReceipt } from "../generated/runtime.ts";
import { validateObjectGraph } from "./objects.ts";
import { indexGraph } from "./objects/graph-index.ts";
import { validateLineage } from "./objects/lineage.ts";
import { validateReferences } from "./objects/references.ts";
import { validateRevisions } from "./objects/revisions.ts";

/** Receipt bytes remain the owner's check; Node checks the supplied binding only. */
export function validateTrialKnowledge(
  binding: TrialKnowledgeBinding, runId: string, trialId: string,
  references: ReadonlyMap<string, OwnerReference>, account?: AccountReceipt,
): void {
  const { registration, view } = binding;
  const consumer = registration.consumer;
  if (consumer.run_id !== runId || consumer.trial_id !== trialId ||
      consumer.required_journal_header !== "runtime_account_initialized/v3" ||
      registration.authority_id !== view.authority_id) {
    throw new Error("Trial Knowledge registration Run/Trial/header/view is incoherent");
  }
  const receipt = references.get(binding.capture_receipt_ref_id);
  if (receipt === undefined || receipt.owner !== "program_store") {
    throw new Error("Trial Knowledge capture receipt requires its exact program_store reference");
  }
  if (account !== undefined && (account.multi_trial !== true || consumer.account_id !== account.account_id ||
      consumer.journal_authority_digest !== account.authority_digest || registration.authority_id !== account.knowledge_authority_id)) {
    throw new Error("Trial Knowledge registration differs from the original v3 account/Journal/authority");
  }
}

function knowledgeBindings(graph: ObjectGraph): void {
  const trials = new Map(graph.objects.filter(row => row.kind === "trial").map(row => [row.trial_id, row]));
  const references = new Map(graph.owner_references.map(reference => [reference.ref_id, reference]));
  for (const trial of trials.values()) {
    const binding = trial.external_snapshot.knowledge;
    if (binding !== undefined) validateTrialKnowledge(binding, trial.run_id, trial.trial_id, references);
  }
  for (const row of graph.objects) {
    if (row.kind === "context_manifest") {
      const binding = trials.get(row.trial.trial_id)?.external_snapshot.knowledge;
      if (binding !== undefined && !row.dependency_ref_ids.includes(binding.capture_receipt_ref_id)) {
        throw new Error("ContextManifest omits the protected Knowledge capture receipt reference");
      }
    }
  }
}

/** Check one detached supplied snapshot; no adoption, persistence or effects. */
export function validateCoherentObjectGraph(value: unknown): ObjectGraph {
  const graph = validateObjectGraph(value);
  const index = indexGraph(graph);
  validateRevisions(index);
  validateLineage(index);
  validateReferences(index);
  knowledgeBindings(graph);
  return graph;
}
