"""Automatic Goal continuation: the limits it states, and the stop after a tool-free
continuation, in both workspace modes. No model service or device is used.

Found in a task-mode trial with no model-call, token or credit limit: after 47
steps in one input the model answered "I've exhausted my budget for this turn"
without a tool call; nothing in the request mentioned a budget. The continuation
now states each limit and its usage, and a continuation that again makes no tool
progress returns control to the user in task mode too (it was project mode only).
"""
from pathlib import Path
import copy
import json
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

from kernelcat_executor.state.goal_progress import limits_text
from kernelcat_executor.contracts.agent import AgentInput
from kernelcat_executor.contracts.models import canonical, digest

UNLIMITED = {"goal_id": "g", "status": "active", "objective": "max operator", "start_seq": 3, "resume_seq": None,
             "max_model_turns": None, "token_budget": None, "max_trials": 1, "max_forks": None,
             "fork_policy": {"enabled": True, "minimum_observations": 3, "cooldown_turns": 3}}


def state(goal=UNLIMITED, **extra):
    value = {"goal": goal, "task_version": 1, "trial_id": "t1", "model_turns": 67, "pending_effect_ids": [], "calls": {},
             "inputs": {}, "budget": {"limit": None, "spent": 247, "reserved": 0}, "trials": [{"start_model_turns": 0}],
             "evidence": [], "max_trial_turns": None, "task": {"contract": {}}, "best": None, "plan": None,
             "goal_usage": {"model_turns": 67, "tokens": 1_830_000, "by_role": {"child": {"model_turns": 19}}}}
    value.update(extra)
    return value


class LimitsTextTests(unittest.TestCase):
    def test_no_limit_set_is_said_outright(self):
        text = limits_text(state(), {"reason": "continue_task"})
        self.assertIn("67 model calls (19 by subagents), no limit set", text)
        self.assertIn("1830000 tokens, no limit set", text)
        self.assertIn("247 credits, no limit set", text)
        self.assertIn("Trial 1 of 1", text)
        self.assertIn("No limit has been reached", text)
        self.assertIn("no per-turn budget", text)

    def test_configured_limits_show_what_is_left(self):
        goal = {**UNLIMITED, "max_model_turns": 200, "token_budget": 2_000_000, "max_trials": None}
        text = limits_text(state(goal, budget={"limit": 500, "spent": 247, "reserved": 3}), {"reason": "continue_task"})
        self.assertIn("67 of 200 model calls (19 by subagents) (133 left)", text)
        self.assertIn("1830000 of 2000000 tokens (170000 left)", text)
        self.assertIn("250 of 500 credits (250 left)", text)
        self.assertNotIn("Trial", text)

    def test_a_fresh_trial_after_the_trial_limit_says_so(self):
        text = limits_text(state(max_trial_turns=40, trials=[{"start_model_turns": 27}]), {"reason": "trial_limit"})
        self.assertIn("40 of 40 model calls in this Trial", text)
        self.assertIn("This Trial reached its model-call limit", text)
        self.assertNotIn("No limit has been reached", text)

    def test_without_usage_falls_back_to_the_model_turn_count(self):
        value = state(); del value["goal_usage"]
        self.assertIn("67 model calls, no limit set", limits_text(value))


class NextInputTests(unittest.TestCase):
    """next_input on a host whose Journal and status are stubbed."""

    def host(self, mode, value, rows=()):
        from kernelcat_worker.application_host import ApplicationHost
        host = object.__new__(ApplicationHost)
        host.commands = []

        def command(kind, data):
            host.commands.append((kind, data))
            return {"input_id": data.get("input_id"), "status": "queued"}
        host.journal = SimpleNamespace(command=command)
        host.config = SimpleNamespace(workspace_mode=mode, goal_review=SimpleNamespace(enabled=False))
        store = SimpleNamespace(events=lambda: list(rows), _events=lambda events: events)
        host.owner = SimpleNamespace(gateway=SimpleNamespace(store=store))
        host.status = lambda: value
        return host

    def continuation(self, mode):
        host = self.host(mode, state())
        decision = host.next_input(SimpleNamespace(worker={"worker_id": "w"}))
        self.assertEqual(decision["action"], "continue")
        accepted = [data for kind, data in host.commands if kind == "accept"]
        self.assertEqual(len(accepted), 1)
        return accepted[0]["text"]

    def test_task_mode_continuation_states_the_limits(self):
        text = self.continuation("task")
        self.assertTrue(text.startswith("Continue the current task from original receipts."))
        self.assertIn("say exactly what is missing", text)
        self.assertIn("Limits for this Goal: 67 model calls (19 by subagents), no limit set", text)
        self.assertIn("the previous turn ended only because it made no tool call", text)
        # The decision stays the last line; the TUI reads its model_turns from there.
        self.assertTrue(text.rsplit("\n", 1)[1].startswith("{"))

    def test_project_mode_continuation_states_the_limits(self):
        text = self.continuation("project")
        self.assertIn("call update_goal with status blocked", text)
        self.assertIn("Limits for this Goal:", text)

    def test_tool_free_continuation_returns_control_in_both_modes(self):
        policy = {"input_id": "loopx-1", "kind": "policy", "status": "completed", "accepted_seq": 10,
                  "applied_seq": 11, "completed_seq": 20}
        # Only protocol rows after the continuation was applied: no tool progress.
        rows = [{"kind": "admitted", "seq": 12, "action": {"tool": "__propose__", "id": "t/step/48/model/ds"}}]
        for mode in ("task", "project"):
            host = self.host(mode, state(inputs={"loopx-1": policy}), rows)
            decision = host.next_input(SimpleNamespace(worker={"worker_id": "w"}))
            self.assertEqual(decision, {"action": "wait", "reason": "no_tool_progress"}, mode)
            self.assertEqual(host.commands[0][0], "goal_outcome")
            self.assertEqual(host.commands[0][1]["status"], "awaiting_user")

    def test_a_continuation_with_tool_progress_continues(self):
        policy = {"input_id": "loopx-1", "kind": "policy", "status": "completed", "accepted_seq": 10,
                  "applied_seq": 11, "completed_seq": 20}
        rows = [{"kind": "admitted", "seq": 13, "action": {"tool": "pipeline_read", "id": "t/step/48/model/ds/call/0"}}]
        host = self.host("task", state(inputs={"loopx-1": policy}), rows)
        self.assertEqual(host.next_input(SimpleNamespace(worker={"worker_id": "w"}))["action"], "continue")

    def test_large_unicode_seed_is_referenced_without_replacing_original_context(self):
        task = {"id": "scatter-max", "scope": "code", "goal": "implement max", "contract": {},
                "seed_files": {"AGENTS.md": "完整的工具与 ABI 指令\n" * 9000,
                               "native-interface.md": "immutable ABI signature"}}
        objective = "完整目标：实现 max，保持精确语义和测量协议。"
        value = state({**UNLIMITED, "objective": objective}, task=task)
        original = copy.deepcopy(value)
        host = self.host("task", value)
        decision = host.next_input(SimpleNamespace(worker={"worker_id": "w"}))
        receipt = next(data for kind, data in host.commands if kind == "accept")
        text = AgentInput(kind="prompt", text=receipt["text"]).text
        facts = json.loads(text.rsplit("\n", 1)[-1])
        self.assertLess(len(text.encode("utf-8")), 65536)
        self.assertNotIn("seed_files", facts["task"])
        self.assertEqual(facts["task"]["id"], task["id"])
        self.assertEqual(facts["task"]["snapshot_sha256"], digest(task))
        self.assertEqual(facts["goal"]["objective"], objective)
        self.assertEqual(facts["decision"]["model_turns"], decision["model_turns"])
        self.assertEqual(receipt["goal_id"], UNLIMITED["goal_id"])
        self.assertEqual(receipt["continuation_format"], "task-reference/v2")
        self.assertEqual(value, original)

    def test_large_results_keep_exact_evidence_lookup_and_original_record_hash(self):
        raw = [{"path": f"run/measurements/part-{i}.log", "sha256": "a" * 64} for i in range(400)]
        best = {"evidence_ref": "evidence/best.json", "source_sha256": "b" * 64, "score": 1.1,
                "raw_data_ref": raw, "result": "large full evidence" * 10000}
        latest = {"evidence_ref": "evidence/failed.json", "raw_data_ref": "original/run.log",
                  "task_version": 1, "evidence_tier": "E0",
                  "error": "actual full compiler failure" * 10000}
        host = self.host("task", state(best=best, evidence=[latest]))
        host.next_input(SimpleNamespace(worker={"worker_id": "w"}))
        text = next(data["text"] for kind, data in host.commands if kind == "accept")
        facts = json.loads(text.rsplit("\n", 1)[-1])
        AgentInput(kind="prompt", text=text)
        self.assertEqual(facts["best"]["record_sha256"], digest(best))
        self.assertEqual(facts["best"]["evidence_ref"], best["evidence_ref"])
        self.assertEqual(facts["best"]["raw_data_ref"]["sha256"], digest(raw))
        self.assertEqual(facts["latest_evidence"]["evidence_ref"], latest["evidence_ref"])
        self.assertEqual(facts["latest_evidence"]["raw_data_ref"], latest["raw_data_ref"])
        self.assertEqual(facts["latest_evidence"]["record_sha256"], digest(latest))
        self.assertNotIn("actual full compiler failure", text)

    def test_oversized_objective_fails_before_policy_acceptance_without_truncation(self):
        objective = "完整目标" * 6000
        value = state({**UNLIMITED, "objective": objective})
        host = self.host("task", value)
        decision = host.next_input(SimpleNamespace(worker={"worker_id": "w"}))
        self.assertEqual(decision["reason"], "continuation_input_invalid")
        self.assertIn("65536", decision["error_summary"])
        self.assertFalse(any(kind == "accept" for kind, _ in host.commands))
        rejected = next(data for kind, data in host.commands if kind == "configuration")
        self.assertEqual(rejected["operation"], "continuation_rejected")
        self.assertGreater(rejected["input_bytes"], 65536)
        outcome = next(data for kind, data in host.commands if kind == "goal_outcome")
        self.assertEqual(outcome["status"], "awaiting_user")
        self.assertEqual(value["goal"]["objective"], objective)
        self.assertNotIn(objective, canonical(rejected))


if __name__ == "__main__":
    unittest.main()
