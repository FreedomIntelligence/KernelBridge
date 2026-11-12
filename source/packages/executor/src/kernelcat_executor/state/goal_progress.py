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
    tokens = 0
    unknown = 0
    for row in events:
        if row["kind"] == "settled" and row.get("action_id") in admitted:
            outcome = row["outcome"]
            usage = outcome.get("data", {}).get("usage") or outcome.get("details", {}).get("usage")
            if isinstance(usage, dict) and usage:
                tokens += token_count(usage)
            else:
                unknown += 1
    return {"model_turns": len(admitted), "tokens": tokens, "usage_unknown_calls": unknown}


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
