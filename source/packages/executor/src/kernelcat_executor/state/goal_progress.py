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
            usage = outcome.get("data", {}).get("usage") or outcome.get("details", {}).get("usage")
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
