"""Goal progress is derived from original receipts, never a second budget ledger."""

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


def continuation_has_progress(events, receipt):
    """Status polling and protocol bookkeeping do not constitute new work."""
    start = receipt.get("applied_seq", receipt["accepted_seq"])
    return any(row["kind"] == "admitted" and row.get("seq", -1) > start
               and not row["action"]["tool"].startswith("__")
               and row["action"]["tool"] != "get_goal" for row in events)
