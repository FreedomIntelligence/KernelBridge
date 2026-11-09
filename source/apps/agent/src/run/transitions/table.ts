import type { TransitionRequest, TransitionRule } from "../../generated/control.ts";

export const TRANSITION_TABLE_VERSION = "control.transitions.v1";

type PolicyRule = TransitionRule extends infer Rule
  ? Rule extends TransitionRule
    ? Omit<Rule, "protocol_version" | "kind" | "table_version" | "rule_id">
    : never
  : never;

// The only policy source. Every edge also requires owner_authorized.
const POLICY = [
  {
    machine: "branch",
    from_states: ["ACTIVE"],
    event: "park",
    actor: "controller",
    to_state: "PARKED",
    required_conditions: [
      "checkpoint_retained",
      "tools_quiescent",
      "lease_epoch_matches",
    ],
    idempotency_domain: "branch_event",
  },
  {
    machine: "branch",
    from_states: ["PARKED"],
    event: "resume",
    actor: "controller",
    to_state: "ACTIVE",
    required_conditions: [
      "checkpoint_retained",
      "context_admitted",
      "run_allows_control",
      "lease_epoch_matches",
    ],
    idempotency_domain: "branch_event",
  },
  {
    machine: "branch",
    from_states: ["ACTIVE", "PARKED"],
    event: "close",
    actor: "controller",
    to_state: "CLOSING",
    required_conditions: [
      "cancellation_recorded",
      "lease_epoch_matches",
    ],
    idempotency_domain: "branch_event",
  },
  {
    machine: "branch",
    from_states: ["CLOSING"],
    event: "resources_stopped",
    actor: "controller",
    to_state: "CLOSED",
    required_conditions: [
      "stop_confirmed",
      "tools_quiescent",
      "lease_epoch_matches",
    ],
    idempotency_domain: "branch_event",
  },
  {
    machine: "session",
    from_states: ["CREATED"],
    event: "start",
    actor: "harness",
    to_state: "RUNNING",
    required_conditions: [
      "context_admitted",
      "workspace_leased",
      "run_allows_control",
      "trial_allows_control",
      "lease_epoch_matches",
    ],
    idempotency_domain: "session_event",
  },
  {
    machine: "session",
    from_states: ["RUNNING"],
    event: "interrupt",
    actor: "controller",
    to_state: "INTERRUPTING",
    required_conditions: [
      "cancellation_recorded",
      "lease_epoch_matches",
    ],
    idempotency_domain: "session_event",
  },
  {
    machine: "session",
    from_states: ["RUNNING", "INTERRUPTING"],
    event: "stopped",
    actor: "harness",
    to_state: "STOPPED",
    required_conditions: [
      "process_stopped",
      "tools_quiescent",
    ],
    idempotency_domain: "session_event",
  },
  {
    machine: "session",
    from_states: ["CREATED"],
    event: "cancel_before_start",
    actor: "controller",
    to_state: "STOPPED",
    required_conditions: [
      "effect_not_sent",
      "cancellation_recorded",
    ],
    idempotency_domain: "session_event",
  },
  {
    machine: "session",
    from_states: ["CREATED", "RUNNING", "INTERRUPTING"],
    event: "failed",
    actor: "harness",
    to_state: "FAILED",
    required_conditions: [
      "failure_recorded",
    ],
    idempotency_domain: "session_event",
  },
  {
    machine: "trial",
    from_states: ["RESERVED"],
    event: "start_generation",
    actor: "controller",
    to_state: "GENERATING",
    required_conditions: [
      "proposal_pinned",
      "external_snapshot_pinned",
      "parent_pinned",
      "reservation_admitted",
      "context_admitted",
      "run_allows_control",
      "lease_epoch_matches",
    ],
    idempotency_domain: "trial_event",
  },
  {
    machine: "trial",
    from_states: ["GENERATING"],
    event: "candidates_frozen",
    actor: "controller",
    to_state: "CANDIDATES_READY",
    required_conditions: [
      "candidates_frozen",
      "generation_recorded",
      "lease_epoch_matches",
    ],
    idempotency_domain: "trial_event",
  },
  {
    machine: "trial",
    from_states: ["CANDIDATES_READY"],
    event: "queue_evaluation",
    actor: "controller",
    to_state: "EVALUATING",
    required_conditions: [
      "candidates_frozen",
      "evaluation_intent_recorded",
      "budget_reserved",
      "run_allows_control",
      "lease_epoch_matches",
    ],
    idempotency_domain: "trial_event",
  },
  {
    machine: "trial",
    from_states: ["CANDIDATES_READY", "EVALUATING"],
    event: "continue_generation",
    actor: "controller",
    to_state: "GENERATING",
    required_conditions: [
      "checkpoint_retained",
      "feedback_recorded",
      "context_admitted",
      "external_snapshot_unchanged",
      "budget_reserved",
      "run_allows_control",
      "lease_epoch_matches",
    ],
    idempotency_domain: "trial_event",
  },
  {
    machine: "trial",
    from_states: ["EVALUATING"],
    event: "evaluation_results_recorded",
    actor: "controller",
    to_state: "CANDIDATES_READY",
    required_conditions: [
      "results_recorded",
      "tools_quiescent",
      "lease_epoch_matches",
    ],
    idempotency_domain: "trial_event",
  },
  {
    machine: "trial",
    from_states: ["CANDIDATES_READY", "EVALUATING"],
    event: "complete",
    actor: "controller",
    to_state: "COMPLETED",
    required_conditions: [
      "outcomes_recorded",
      "tools_quiescent",
      "lease_epoch_matches",
    ],
    idempotency_domain: "trial_event",
  },
  {
    machine: "trial",
    from_states: ["RESERVED", "GENERATING", "CANDIDATES_READY", "EVALUATING"],
    event: "cancel",
    actor: "controller",
    to_state: "CANCELLED",
    required_conditions: [
      "cancellation_recorded",
      "lease_epoch_matches",
    ],
    idempotency_domain: "trial_event",
  },
  {
    machine: "trial",
    from_states: ["RESERVED", "GENERATING", "CANDIDATES_READY", "EVALUATING"],
    event: "failed",
    actor: "controller",
    to_state: "FAILED",
    required_conditions: [
      "failure_recorded",
      "lease_epoch_matches",
    ],
    idempotency_domain: "trial_event",
  },
  {
    machine: "evaluation",
    from_states: ["QUEUED"],
    event: "start",
    actor: "scheduler",
    to_state: "RUNNING",
    required_conditions: [
      "candidate_frozen",
      "dispatch_intent_recorded",
      "resource_lease_valid",
      "budget_reserved",
    ],
    idempotency_domain: "evaluation_event",
  },
  {
    machine: "evaluation",
    from_states: ["RUNNING"],
    event: "observation_success",
    actor: "controller",
    to_state: "SUCCEEDED",
    required_conditions: [
      "observation_recorded",
      "observation_identity_valid",
      "success_recorded",
    ],
    idempotency_domain: "evaluation_event",
  },
  {
    machine: "evaluation",
    from_states: ["RUNNING"],
    event: "candidate_failure",
    actor: "controller",
    to_state: "INVALID_CANDIDATE",
    required_conditions: [
      "observation_recorded",
      "observation_identity_valid",
      "candidate_failure_recorded",
    ],
    idempotency_domain: "evaluation_event",
  },
  {
    machine: "evaluation",
    from_states: ["QUEUED", "RUNNING"],
    event: "infrastructure_failure",
    actor: "controller",
    to_state: "INFRA_ERROR",
    required_conditions: [
      "observation_recorded",
      "observation_identity_valid",
      "infrastructure_failure_recorded",
    ],
    idempotency_domain: "evaluation_event",
  },
  {
    machine: "evaluation",
    from_states: ["QUEUED"],
    event: "cancel_before_start",
    actor: "controller",
    to_state: "CANCELLED",
    required_conditions: [
      "effect_not_sent",
      "cancellation_recorded",
    ],
    idempotency_domain: "evaluation_event",
  },
  {
    machine: "evaluation",
    from_states: ["RUNNING"],
    event: "cancellation_confirmed",
    actor: "controller",
    to_state: "CANCELLED",
    required_conditions: [
      "stop_confirmed",
      "cancellation_recorded",
    ],
    idempotency_domain: "evaluation_event",
  },
  {
    machine: "settlement",
    from_states: ["PENDING"],
    event: "reconcile",
    actor: "budget_ledger",
    to_state: "RECONCILING",
    required_conditions: [
      "owner_execution_ended",
      "reservation_retained",
    ],
    idempotency_domain: "settlement_revision",
  },
  {
    machine: "settlement",
    from_states: ["PENDING", "RECONCILING"],
    event: "settle",
    actor: "budget_ledger",
    to_state: "SETTLED",
    required_conditions: [
      "costs_reconciled",
      "no_unresolved_effects",
    ],
    idempotency_domain: "settlement_revision",
  },
  {
    machine: "settlement",
    from_states: ["SETTLED"],
    event: "usage_revision_recorded",
    actor: "budget_ledger",
    to_state: "RECONCILING",
    required_conditions: [
      "usage_revision_recorded",
      "reconciliation_required",
    ],
    idempotency_domain: "settlement_revision",
  },
  {
    machine: "context",
    from_states: ["PROPOSED"],
    event: "activate",
    actor: "controller",
    to_state: "ACTIVE",
    required_conditions: [
      "parent_matches",
      "branch_revision_matches",
      "context_revision_matches",
      "dependencies_valid",
      "lease_epoch_matches",
      "tools_quiescent",
      "budget_reserved",
      "dispatch_intent_recorded",
      "run_allows_control",
      "trial_allows_control",
    ],
    idempotency_domain: "context_event",
  },
  {
    machine: "context",
    from_states: ["PROPOSED", "ACTIVE"],
    event: "invalidate",
    actor: "controller",
    to_state: "INVALIDATED",
    required_conditions: [
      "invalidation_recorded",
    ],
    idempotency_domain: "context_event",
  },
  {
    machine: "context",
    from_states: ["PROPOSED", "ACTIVE"],
    event: "cancel",
    actor: "controller",
    to_state: "CANCELLED",
    required_conditions: [
      "cancellation_recorded",
    ],
    idempotency_domain: "context_event",
  },
  {
    machine: "external_call",
    from_states: ["INTENT_RECORDED"],
    event: "dispatch",
    actor: "scheduler",
    to_state: "DISPATCHED",
    required_conditions: [
      "dispatch_intent_recorded",
      "reservation_retained",
      "context_current",
      "dependencies_valid",
      "lease_epoch_matches",
      "run_allows_control",
      "trial_allows_control",
    ],
    idempotency_domain: "operation_event",
  },
  {
    machine: "external_call",
    from_states: ["INTENT_RECORDED"],
    event: "cancel_before_send",
    actor: "controller",
    to_state: "CANCELLED",
    required_conditions: [
      "effect_not_sent",
      "cancellation_recorded",
    ],
    idempotency_domain: "operation_event",
  },
  {
    machine: "external_call",
    from_states: ["DISPATCHED"],
    event: "result_success",
    actor: "controller",
    to_state: "SUCCEEDED",
    required_conditions: [
      "outcome_recorded",
    ],
    idempotency_domain: "operation_event",
  },
  {
    machine: "external_call",
    from_states: ["DISPATCHED"],
    event: "result_failure",
    actor: "controller",
    to_state: "FAILED",
    required_conditions: [
      "outcome_recorded",
    ],
    idempotency_domain: "operation_event",
  },
  {
    machine: "external_call",
    from_states: ["DISPATCHED"],
    event: "outcome_uncertain",
    actor: "controller",
    to_state: "UNKNOWN_OUTCOME",
    required_conditions: [
      "uncertainty_recorded",
      "reservation_retained",
    ],
    idempotency_domain: "operation_event",
  },
  {
    machine: "external_call",
    from_states: ["DISPATCHED"],
    event: "cancellation_confirmed",
    actor: "controller",
    to_state: "CANCELLED",
    required_conditions: [
      "stop_confirmed",
      "cancellation_recorded",
    ],
    idempotency_domain: "operation_event",
  },
  {
    machine: "external_call",
    from_states: ["UNKNOWN_OUTCOME"],
    event: "reconcile_success",
    actor: "reconciler",
    to_state: "SUCCEEDED",
    required_conditions: [
      "reconciliation_recorded",
      "reservation_retained",
    ],
    idempotency_domain: "operation_event",
  },
  {
    machine: "external_call",
    from_states: ["UNKNOWN_OUTCOME"],
    event: "reconcile_failure",
    actor: "reconciler",
    to_state: "FAILED",
    required_conditions: [
      "reconciliation_recorded",
      "reservation_retained",
    ],
    idempotency_domain: "operation_event",
  },
  {
    machine: "external_call",
    from_states: ["UNKNOWN_OUTCOME"],
    event: "reconcile_cancelled",
    actor: "reconciler",
    to_state: "CANCELLED",
    required_conditions: [
      "reconciliation_recorded",
      "stop_confirmed",
      "reservation_retained",
    ],
    idempotency_domain: "operation_event",
  },
] satisfies readonly PolicyRule[];

function freezeRule(rule: TransitionRule): TransitionRule {
  Object.freeze(rule.from_states);
  Object.freeze(rule.required_conditions);
  return Object.freeze(rule);
}

const RULES: readonly TransitionRule[] = Object.freeze(POLICY.map((rule) => freezeRule({
  protocol_version: "kernelcat.lab-control/v1",
  kind: "transition_rule",
  table_version: TRANSITION_TABLE_VERSION,
  rule_id: `${rule.machine}.${rule.event}`,
  ...rule,
  required_conditions: ["owner_authorized", ...rule.required_conditions],
})));

function tupleKey(parts: readonly string[]): string {
  return JSON.stringify(parts);
}

function indexRules(): {
  readonly edges: ReadonlyMap<string, TransitionRule>;
  readonly events: ReadonlySet<string>;
} {
  const edges = new Map<string, TransitionRule>();
  const events = new Set<string>();
  const ruleIds = new Set<string>();
  for (const rule of RULES) {
    if (ruleIds.has(rule.rule_id)) {
      throw new Error(`Duplicate transition rule ${rule.rule_id}`);
    }
    ruleIds.add(rule.rule_id);
    events.add(tupleKey([rule.machine, rule.event]));
    for (const state of rule.from_states) {
      const key = tupleKey([rule.machine, state, rule.event]);
      if (edges.has(key)) {
        throw new Error(`Overlapping transition edge ${key}`);
      }
      edges.set(key, rule);
    }
  }
  return { edges, events };
}

const INDEX = indexRules();

export type RuleLookup =
  | { readonly status: "unknown_event" | "illegal_transition" }
  | { readonly status: "matched"; readonly rule: TransitionRule };

/** Constant-time lookup in the fixed, module-initialized policy index. */
export function lookupTransitionRule(request: TransitionRequest): RuleLookup {
  if (!INDEX.events.has(tupleKey([request.machine, request.event]))) {
    return { status: "unknown_event" };
  }
  const rule = INDEX.edges.get(tupleKey([request.machine, request.state, request.event]));
  if (rule === undefined) {
    return { status: "illegal_transition" };
  }
  return { status: "matched", rule };
}

/** Each inspection gets detached rules; callers cannot change the policy. */
export function describeTransitionRules(): readonly TransitionRule[] {
  return Object.freeze(RULES.map((rule) => freezeRule(structuredClone(rule))));
}
