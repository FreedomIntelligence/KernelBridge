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


def continuation_has_progress(events, receipt):
    """Status polling and protocol bookkeeping do not constitute new work."""
    start = receipt.get("applied_seq", receipt["accepted_seq"])
    return any(row["kind"] == "admitted" and row.get("seq", -1) > start
               and not row["action"]["tool"].startswith("__")
               and row["action"]["tool"] != "get_goal" for row in events)


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
