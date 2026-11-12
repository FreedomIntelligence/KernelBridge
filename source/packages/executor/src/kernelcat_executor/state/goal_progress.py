"""Goal progress is derived from original receipts, never a second budget ledger."""
import json


def review_verdict(outcome):
    if outcome.get("status") != "completed":
        raise ValueError("Goal reviewer did not return a completed model receipt")
    blocks = outcome.get("data", {}).get("content", [])
    if not blocks or any(block.get("type") != "text" for block in blocks):
        raise ValueError("Goal reviewer must return text without tools")
    value = json.loads("\n".join(block["text"] for block in blocks))
    if (not isinstance(value, dict) or set(value) != {"status", "summary"}
            or value["status"] not in {"met", "continue", "blocked"}
            or not isinstance(value["summary"], str) or not value["summary"].strip()
            or len(value["summary"]) > 4096):
        raise ValueError("Goal reviewer returned an invalid verdict")
    return value

def token_count(usage):
    def number(key):
        value = usage.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    if isinstance(usage.get("total_tokens"), int):
        return number("total_tokens")
    if "prompt_tokens" in usage:
        return number("prompt_tokens") + number("completion_tokens")
    return sum(number(key) for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def goal_usage(events, goal):
    start = goal.get("start_seq", -1)
    admitted = {row["action"]["id"]: row for row in events if row["kind"] == "admitted"
                and row.get("seq", -1) > start and row["action"]["tool"] == "__propose__"}
    groups = {name: {'model_turns': 0, 'tokens': 0, 'usage_unknown_calls': 0, 'pending_calls': 0}
              for name in ('parent', 'compact', 'child', 'review')}
    def category(action):
        if '/compact/' in action['id']:
            return 'compact'
        if '/goal-review/' in action['id']:
            return 'review'
        return 'child' if action['task'].endswith('/leaf') else 'parent'
    for row in admitted.values():
        group = groups[category(row['action'])]
        group['model_turns'] += 1
        group['pending_calls'] += 1
    tokens = 0
    unknown = 0
    for row in events:
        if row["kind"] == "settled" and row.get("action_id") in admitted:
            outcome = row["outcome"]
            group = groups[category(admitted[row['action_id']]['action'])]
            group['pending_calls'] -= 1
            data = outcome.get("data", {})
            failure_details = data.get("failure_details")
            usage = (data.get("usage") or outcome.get("details", {}).get("usage")
                     or (failure_details.get("usage") if isinstance(failure_details, dict) else None))
            if isinstance(usage, dict) and usage:
                tokens += token_count(usage)
                group['tokens'] += token_count(usage)
            else:
                unknown += 1
                group['usage_unknown_calls'] += 1
    return {"model_turns": len(admitted), "tokens": tokens, "usage_unknown_calls": unknown,
            'by_role': groups, 'counting': 'all_model_calls_including_compact_child_review'}


# Reading the Goal or rewriting the plan is bookkeeping, not work (Codex: "a plan
# update is not progress").
BOOKKEEPING_TOOLS = frozenset({"get_goal", "update_plan"})


def continuation_has_progress(events, receipt):
    """Status polling and protocol bookkeeping do not constitute new work."""
    start = receipt.get("applied_seq", receipt["accepted_seq"])
    return any(row["kind"] == "admitted" and row.get("seq", -1) > start
               and not row["action"]["tool"].startswith("__")
               and row["action"]["tool"] not in BOOKKEEPING_TOOLS for row in events)


PLAN_STATUSES = ("pending", "in_progress", "completed")
MAX_PLAN_STEPS = 12
MAX_STEP_CHARS = 80


class PlanError(ValueError):
    """An update_plan call the host refuses; the message is returned to the model."""


def validate_plan(items):
    """Whole-plan replacement in Codex's shape, checked by the host (Codex only documents the rules)."""
    if not isinstance(items, list) or not items:
        raise PlanError("plan must contain at least one step")
    if len(items) > MAX_PLAN_STEPS:
        raise PlanError(f"plan has {len(items)} steps; keep it to at most {MAX_PLAN_STEPS}")
    plan = []
    for item in items:
        step = " ".join(str((item or {}).get("step", "")).split())
        status = (item or {}).get("status")
        if not step:
            raise PlanError("plan steps must not be empty")
        if len(step) > MAX_STEP_CHARS:
            raise PlanError(f"plan step exceeds {MAX_STEP_CHARS} characters; keep steps short and checkable")
        if status not in PLAN_STATUSES:
            raise PlanError("plan step status must be pending, in_progress or completed")
        plan.append({"step": step, "status": status})
    if sum(item["status"] == "in_progress" for item in plan) > 1:
        raise PlanError("at most one plan step may be in_progress")
    return plan


def current_plan(actions, goal):
    """The latest accepted update_plan receipt of this Goal; the receipt is the record."""
    if not goal:
        return None
    latest = None
    for key, item in actions.items():
        if item["action"]["tool"] != "update_plan" or item.get("seq", -1) <= goal.get("start_seq", -1):
            continue
        outcome = item.get("outcome") or {}
        data = outcome.get("data") or {}
        if outcome.get("status") == "completed" and data.get("goal_id") == goal["goal_id"] and data.get("plan"):
            latest = {**data, "action_id": key}
    return latest


def plan_text(plan):
    """Compact plan for model-facing continuation and review prompts."""
    marks = {"completed": "x", "in_progress": ">", "pending": " "}
    done = sum(item["status"] == "completed" for item in plan["plan"])
    return f"({done}/{len(plan['plan'])} complete) " + "; ".join(f"[{marks[item['status']]}] {item['step']}" for item in plan["plan"])


def limits_text(state, decision=None):
    """The Goal's limits and usage in words, for the automatic continuation.

    A null limit means none is configured. Models read a null field (or a missing
    one) as an exhausted allowance often enough to stop with "budget exhausted";
    the continuation therefore states each limit and its usage outright. It is
    written only when the policy chose to continue or to start a fresh Trial, so
    the only limit that can have been reached is the Trial's own (trial_limit).
    """
    goal = state.get("goal") or {}
    usage = state.get("goal_usage") or {"model_turns": state.get("model_turns", 0), "tokens": 0}
    budget = state.get("budget") or {}
    child = ((usage.get("by_role") or {}).get("child") or {}).get("model_turns", 0)

    def bounded(used, limit, noun):
        return f"{used} {noun}, no limit set" if limit is None else f"{used} of {limit} {noun} ({max(0, limit - used)} left)"
    parts = [bounded(usage.get("model_turns", 0), goal.get("max_model_turns"),
                     "model calls" + (f" ({child} by subagents)" if child else "")),
             bounded(usage.get("tokens", 0), goal.get("token_budget"), "tokens"),
             bounded(budget.get("spent", 0) + budget.get("reserved", 0), budget.get("limit"), "credits")]
    trials = state.get("trials") or []
    if state.get("max_trial_turns") is not None:
        used = state.get("model_turns", 0) - (trials[-1].get("start_model_turns", 0) if trials else 0)
        parts.append(f"{used} of {state['max_trial_turns']} model calls in this Trial")
    if goal.get("max_trials") is not None:
        parts.append(f"Trial {max(1, len(trials))} of {goal['max_trials']}")
    reached = (decision or {}).get("reason") == "trial_limit"
    return ("Limits for this Goal: " + "; ".join(parts) + ". "
            + ("This Trial reached its model-call limit, so work continues in a fresh Trial; no other limit has been reached. "
               if reached else "No limit has been reached. ")
            + "There is no per-turn budget: the previous turn ended only because it made no tool call. "
            "Do not report a budget, allowance or turn limit as exhausted unless a limit above says so.")


def model_call_progress(store):
    """Read one original Journal snapshot; never reserve or authorize a call.

    application_state identifies the bound application only. Its Goal and usage
    are both projected again from the same events snapshot to avoid mixing a
    replaced Goal with the preceding Goal's usage. No default ceiling is invented.
    """
    application_state = getattr(store, "application_state", None)
    application = application_state() if application_state is not None else None
    if application is None:
        return None
    from kernelcat_executor.state.application import project
    from kernelcat_executor.state.journal import Journal
    rows = store.events()
    state = project(rows, application["application_session_id"])
    goal = state.get("goal")
    # Only an active Goal can admit a model call under this ceiling. Inactive
    # Goals remain history, not a budget for subsequent ordinary codegen.
    limit = goal.get("max_model_turns") if goal and goal.get("status") == "active" else None
    if type(limit) is not int or limit < 1:
        return None
    used = goal_usage(list(Journal._events(rows)), goal)["model_turns"]
    # The request is prepared before admission. Never pretend that this
    # observation reserves its slot, or change the input after it is committed.
    return {"goal_id": goal.get("goal_id"), "observed_seq": state["cursor"],
        "max_model_turns": limit, "model_calls_admitted": used,
        "remaining_calls_including_this_request": max(0, limit - used), "current_request_cost": 1,
        "counting": "all_model_calls_including_compact_child_review",
        "concurrent_calls_may_consume_remaining": True}


def with_model_call_progress(context, store):
    """Attach or refresh trusted metadata before compilation and input hashing."""
    value = dict(context)
    value.pop("goal_progress", None)
    progress = model_call_progress(store)
    if progress is not None:
        value["goal_progress"] = progress
    return value
