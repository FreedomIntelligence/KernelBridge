import type {
  TransitionRejectionCode,
  TransitionRequest,
  TransitionRule,
} from "../generated/control.ts";
import { validateControlMessage } from "./control.ts";
import {
  describeTransitionRules,
  lookupTransitionRule,
  TRANSITION_TABLE_VERSION,
} from "./transitions/table.ts";

export { describeTransitionRules };

/** A supplied request and its legal edge, never an adopted or committed state. */
export interface TransitionPlan {
  readonly kind: "transition_plan";
  readonly request: TransitionRequest;
  readonly rule: TransitionRule;
  readonly proposed_event_identity: string;
}

export interface TransitionRejectionContext {
  readonly machine: TransitionRequest["machine"];
  readonly run_id: string;
  readonly object_id: string;
  readonly state: TransitionRequest["state"];
  readonly event: string;
  readonly event_id: string;
  readonly condition?: string;
  readonly reason?: string;
}

export class TransitionPlanningError extends Error {
  readonly code: TransitionRejectionCode;
  readonly context: TransitionRejectionContext;

  constructor(code: TransitionRejectionCode, detail: string, context: TransitionRejectionContext) {
    super(`${code}: ${context.machine}/${context.object_id} event ${context.event} (${context.event_id}): ${detail}`);
    this.name = "TransitionPlanningError";
    this.code = code;
    this.context = Object.freeze({ ...context });
  }
}

function reject(
  request: TransitionRequest,
  code: TransitionRejectionCode,
  detail: string,
  condition?: string,
  reason?: string,
): never {
  throw new TransitionPlanningError(code, detail, {
    machine: request.machine,
    run_id: request.run_id,
    object_id: request.object_id,
    state: request.state,
    event: request.event,
    event_id: request.event_id,
    ...(condition === undefined ? {} : { condition }),
    ...(reason === undefined ? {} : { reason }),
  });
}

function requireConditions(request: TransitionRequest, rule: TransitionRule): void {
  for (const condition of rule.required_conditions) {
    const observation = request.conditions[condition];
    if (!Object.hasOwn(request.conditions, condition)) {
      reject(request, "missing_condition", `required condition ${condition} is missing`, condition);
    }
    if (typeof observation === "object") {
      reject(request, "unknown_condition", `${condition} is unknown: ${observation.reason}`, condition, observation.reason);
    }
    if (observation !== true) {
      reject(request, "unsatisfied_condition", `${condition} must be true`, condition);
    }
  }
}

function selectRule(request: TransitionRequest): TransitionRule {
  if (request.table_version !== TRANSITION_TABLE_VERSION) {
    reject(request, "table_version_mismatch", `expected ${TRANSITION_TABLE_VERSION}, received ${request.table_version}`);
  }
  if (request.expected_revision !== request.revision) {
    reject(request, "revision_conflict", `expected revision ${request.expected_revision}, supplied revision ${request.revision}`);
  }
  const lookup = lookupTransitionRule(request);
  if (lookup.status !== "matched") {
    reject(request, lookup.status, `no ${request.event} edge from ${request.state}`);
  }
  if (request.actor !== lookup.rule.actor) {
    reject(request, "wrong_actor", `requires ${lookup.rule.actor}, received ${request.actor}`);
  }
  return lookup.rule;
}

/** Validate once, then plan from detached observations without effects or storage. */
export function planStateTransition(value: unknown): TransitionPlan {
  const request = validateControlMessage(value);
  if (request.kind !== "transition_request") {
    throw new Error(`Expected transition_request, received ${request.kind}`);
  }
  const rule = selectRule(request);
  requireConditions(request, rule);
  return {
    kind: "transition_plan",
    request,
    rule: structuredClone(rule),
    // JSON tuple encoding preserves boundaries even when identifiers contain '/'.
    proposed_event_identity: JSON.stringify([
      rule.idempotency_domain, request.run_id, request.object_id, request.event_id,
    ]),
  };
}
