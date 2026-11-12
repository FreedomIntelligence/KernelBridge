"""Bounded continuation cues; original task instructions remain source-bound."""
from kernelcat_executor.contracts.agent import AgentInput
from kernelcat_executor.contracts.models import canonical, digest
from kernelcat_executor.state.goal_progress import limits_text, plan_text


def evidence_reference(value):
    if not value:
        return None
    # Keep exact lookup identities, not potentially large result/error payloads.
    reference = {"record_sha256": digest(value), **{key: value[key] for key in (
        "evidence_ref", "evidence_tier", "source_sha256", "trial_id", "task_version",
        "measurement_profile_version", "score") if key in value}}
    if "raw_data_ref" in value:
        raw = value["raw_data_ref"]
        reference["raw_data_ref"] = raw if len(canonical(raw).encode("utf-8")) <= 8192 else {
            "sha256": digest(raw), "location": "raw_data_ref in the original evidence record",
            "evidence_ref": value.get("evidence_ref")}
    return reference


def continuation_text(state, decision, workspace_mode):
    task = state["task"]
    goal = state["goal"]
    facts = {
        "task": {"id": task.get("id"), "task_version": state["task_version"],
                 "snapshot_sha256": digest(task),
                 "instructions": "Retained original task/instruction snapshot and admitted workspace files; not replaced by this continuation."},
        "goal": {key: goal[key] for key in ("goal_id", "objective", "status") if key in goal},
        "budget": state["budget"], "best": evidence_reference(state.get("best")),
        "latest_evidence": evidence_reference(state.get("evidence", [])[-1]) if state.get("evidence") else None,
        "decision": decision,
    }
    if workspace_mode == "project":
        text = ("Continue the explicitly requested coding Goal. Implement and verify the remaining work. "
                "Use get_goal to inspect progress when needed. When all requested work is verified, call update_goal "
                "with status complete and a summary of actual checks (finish_goal is also supported). "
                "If progress requires missing user input, call update_goal with status blocked and explain what is needed. "
                "Do not call a missing device result verified. A tool-free automatic turn returns control to the user.\n")
        if state.get("plan"):
            text += f"Current plan {plan_text(state['plan'])}. Continue from the in-progress step; a plan update alone is not progress.\n"
    else:
        text = ("Continue the current task from original receipts. "
                "Complete the declared correctness/performance acceptance; do not treat a single timing as a paired gain. "
                "Inspect missing or failed measurements before diagnosing stagnation. If you cannot proceed without the "
                "user, say exactly what is missing; a tool-free automatic turn returns control to the user.\n")
    text += ("The full original task, AGENTS instructions and ABI remain in the admitted instruction snapshot and workspace files. "
             "Read those files and the exact evidence references for details; this cue does not replace or shorten them.\n")
    # The decision stays in the last JSON line for the TUI continuation label.
    text += limits_text(state, decision) + "\n" + canonical(facts)
    return AgentInput(kind="prompt", text=text).text


def input_validation_failure(text, error, *, accepted=True):
    """Retain the contract error without echoing or truncating the input itself."""
    errors = [{"location": list(item["loc"]), "type": item["type"], "message": item["msg"]}
              for item in error.errors(include_input=False, include_url=False)]
    if text is None:
        for item in error.errors(include_url=False):
            value = item.get("input")
            candidate = value.get("text") if isinstance(value, dict) else value
            if isinstance(candidate, str):
                text = candidate; break
    size = len(text.encode("utf-8")) if isinstance(text, str) else None
    retained = ("Original accepted text is retained in its input receipt." if accepted else
                "No continuation/input receipt was created; the original task and full Goal objective are retained.")
    return {"reason": "input_validation_failed", "input_bytes": size, "validation_errors": errors,
            "error_summary": "Input rejected before executor admission: " + "; ".join(item["message"] for item in errors)
                             + f" (UTF-8 bytes: {size}; limit: 65536). " + retained}
