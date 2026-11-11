"""LoopX application policy: recommendations over Journal facts, never a budget ledger."""
from kernelcat_executor.contracts.models import digest


POLICY_VERSION = "loopx.application/v1"


def recommend(state: dict) -> dict:
    goal = state.get("goal")
    base = {"policy_version": POLICY_VERSION, "task_version": state["task_version"],
            "trial_id": state.get("trial_id"), "model_turns": state["model_turns"]}
    if not goal or goal["status"] != "active":
        return {**base, "action": "wait", "reason": "goal_inactive"}
    if state["pending_effect_ids"] or state["calls"]:
        return {**base, "action": "wait", "reason": "reconciliation"}
    if any(item["status"] in {"queued", "applied"} and item["kind"] != "control" for item in state["inputs"].values()):
        return {**base, "action": "wait", "reason": "accepted_input"}
    budget = state["budget"]
    if (goal.get("max_model_turns") is not None and state["model_turns"] >= goal["max_model_turns"]
            or budget["limit"] is not None and budget["spent"] + budget["reserved"] >= budget["limit"]):
        return {**base, "action": "stop", "reason": "budget_limit"}
    trials = state["trials"]
    trial_turns = state["model_turns"] - (trials[-1].get("start_model_turns", 0) if trials else 0)
    policy = goal.get("fork_policy", {})
    window_size = policy.get("minimum_observations", 3)
    window = [item for item in state["evidence"] if item["task_version"] == state["task_version"]
              and item["evidence_tier"] in {"E2", "E3"}][-window_size:]
    measured = len(window) == window_size and len({item["measurement_profile_version"] for item in window}) == 1
    noise = window[-1].get("noise_threshold") if measured else None
    plateau = (measured and all(item["evidence_tier"] == "E2" for item in window)
               and isinstance(noise, (int, float)) and noise >= 0
               and all(isinstance(item.get("score"), (int, float)) for item in window)
               and max(item["score"] for item in window) - min(item["score"] for item in window) <= noise)
    cooldown = trial_turns >= policy.get("cooldown_turns", 3)
    trial_limit = state["max_trial_turns"] is not None and trial_turns >= state["max_trial_turns"]
    reset = trial_limit or (policy.get("enabled", True) and plateau and cooldown)
    if reset and ((goal.get("max_trials") is not None and len(trials) >= goal["max_trials"])
                  or (goal.get("max_forks") is not None and max(0, len(trials) - 1) >= goal["max_forks"])):
        return {**base, "action": "stop", "reason": "budget_limit"}
    result = {**base, "action": "reset" if reset else "continue",
              "reason": "trial_limit" if trial_limit else "measured_plateau" if reset else "continue_task",
              "evidence_refs": [item["evidence_ref"] for item in window]}
    result["decision_id"] = digest(result)
    return result
