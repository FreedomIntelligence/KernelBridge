"""Goal plan (update_plan): host validation, receipt-derived current plan, the
progress rule, the host handler and the completion reviewer's input.
No model service or device is used."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import Policy, ToolFailure
from kernelcat_executor.state.goal_progress import (
    MAX_PLAN_STEPS, PlanError, continuation_has_progress, current_plan, plan_text, validate_plan)


def step(text, status="pending"):
    return {"step": text, "status": status}


class PlanValidationTests(unittest.TestCase):
    def test_accepts_a_whole_plan_and_normalizes_whitespace(self):
        plan = validate_plan([step("  read   the code ", "completed"), step("fix it", "in_progress"), step("test")])
        self.assertEqual(plan[0], {"step": "read the code", "status": "completed"})
        self.assertEqual([item["status"] for item in plan], ["completed", "in_progress", "pending"])

    def test_refuses_what_codex_only_documents(self):
        cases = {
            "at most one plan step may be in_progress": [step("a", "in_progress"), step("b", "in_progress")],
            "must not be empty": [step("   ")],
            "at least one step": [],
            "80 characters": [step("x" * 81)],
            "pending, in_progress or completed": [step("a", "done")],
            f"at most {MAX_PLAN_STEPS}": [step(str(i)) for i in range(MAX_PLAN_STEPS + 1)],
        }
        for message, plan in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(PlanError, message):
                validate_plan(plan)

    def test_plan_text_is_compact(self):
        text = plan_text({"plan": [step("a", "completed"), step("b", "in_progress"), step("c")]})
        self.assertEqual(text, "(1/3 complete) [x] a; [>] b; [ ] c")


class CurrentPlanTests(unittest.TestCase):
    goal = {"goal_id": "g2", "start_seq": 10}

    def action(self, seq, goal_id, plan, status="completed", tool="update_plan"):
        return {"seq": seq, "action": {"tool": tool}, "outcome": {"status": status, "data": {"goal_id": goal_id, "plan": plan}}}

    def test_latest_accepted_receipt_of_this_goal_wins(self):
        actions = {
            "old-goal": self.action(5, "g1", [step("before")]),
            "first": self.action(12, "g2", [step("one")]),
            "other-goal": self.action(13, "g1", [step("stale")]),
            "refused": self.action(14, "g2", [step("bad")], status="failed"),
            "read": self.action(15, "g2", [step("x")], tool="read"),
            "second": self.action(16, "g2", [step("one", "completed"), step("two", "in_progress")]),
        }
        latest = current_plan(actions, self.goal)
        self.assertEqual(latest["action_id"], "second")
        self.assertEqual([item["step"] for item in latest["plan"]], ["one", "two"])

    def test_no_goal_or_no_plan(self):
        self.assertIsNone(current_plan({}, self.goal))
        self.assertIsNone(current_plan({"a": self.action(12, "g2", [step("x")])}, None))


class ProgressRuleTests(unittest.TestCase):
    def rows(self, *tools):
        return [{"kind": "admitted", "seq": 20 + index, "action": {"tool": tool}} for index, tool in enumerate(tools)]

    def test_plan_updates_and_goal_reads_are_not_progress(self):
        receipt = {"accepted_seq": 19}
        self.assertFalse(continuation_has_progress(self.rows("update_plan", "get_goal", "__propose__"), receipt))
        self.assertTrue(continuation_has_progress(self.rows("update_plan", "read"), receipt))


class HostHandlerTests(unittest.TestCase):
    def host(self, goal):
        from kernelcat_worker.application_host import ApplicationHost
        host = object.__new__(ApplicationHost)
        host.journal = SimpleNamespace(state=lambda: {"goal": goal})
        return host

    def test_records_the_plan_for_the_active_goal(self):
        host = self.host({"goal_id": "g", "status": "active"})
        outcome = host.update_codegen_plan(SimpleNamespace(arguments={"explanation": " split  the fix ",
            "plan": [step("reproduce", "completed"), step("fix", "in_progress"), step("test")]}))
        self.assertEqual(outcome.data["goal_id"], "g")
        self.assertEqual((outcome.data["completed"], outcome.data["total"]), (1, 3))
        self.assertEqual(outcome.data["explanation"], "split the fix")

    def test_refusals_are_tool_failures_the_model_can_correct(self):
        with self.assertRaisesRegex(ToolFailure, "only while an explicit Goal is active"):
            self.host(None).update_codegen_plan(SimpleNamespace(arguments={"plan": [step("a")]}))
        with self.assertRaisesRegex(ToolFailure, "only while an explicit Goal is active"):
            self.host({"goal_id": "g", "status": "completed"}).update_codegen_plan(SimpleNamespace(arguments={"plan": [step("a")]}))
        with self.assertRaisesRegex(ToolFailure, "in_progress"):
            self.host({"goal_id": "g", "status": "active"}).update_codegen_plan(
                SimpleNamespace(arguments={"plan": [step("a", "in_progress"), step("b", "in_progress")]}))


class ReviewerInputTests(unittest.TestCase):
    def test_reviewer_sees_the_final_plan_as_a_checklist_not_evidence(self):
        from kernelcat_worker import application_goal_review as review_module
        captured = {}

        class Stop(Exception):
            pass

        def propose(*args, harness, **kwargs):
            captured["harness"] = harness
            raise Stop()
        store = SimpleNamespace(snapshot=lambda: {"actions": {}}, policy=lambda: Policy(max_steps=4))
        host = SimpleNamespace(owner=SimpleNamespace(gateway=SimpleNamespace(store=store), proposers={store.policy().routes[0]: None}),
                               task=SimpleNamespace(id="task"), config=SimpleNamespace(goal_review=SimpleNamespace(context_bytes=65536)))
        plan = {"plan": [step("reproduce", "completed"), step("fix")], "explanation": "two steps"}
        state = {"goal": {"goal_id": "g", "objective": "fix the bug", "start_seq": 0}, "configurations": [], "plan": plan}
        with mock.patch.object(review_module, "propose", propose), self.assertRaises(Stop):
            review_module.review(host, SimpleNamespace(worker={}), state, {"input_id": "i1"})
        harness = captured["harness"]
        self.assertIn("is not evidence", harness["system_prompt"])
        content = harness["messages"][0]["content"]
        self.assertIn('"plan":{"explanation":"two steps","steps":[', content)
        self.assertIn('"status":"pending","step":"fix"', content)


class CompactionTests(unittest.TestCase):
    def test_compaction_summary_restates_the_active_plan(self):
        from kernelcat_worker.application_host import ApplicationHost
        from kernelcat_executor.contracts.models import Outcome
        host = object.__new__(ApplicationHost)
        store = SimpleNamespace(policy=lambda: Policy(max_steps=4))
        host.owner = SimpleNamespace(gateway=SimpleNamespace(store=store), proposers={store.policy().routes[0]: None})
        host.task = SimpleNamespace(id="task")
        plan = {"plan": [step("reproduce", "completed"), step("fix", "in_progress")]}
        state = {"goal": {"goal_id": "g", "status": "active"}, "plan": plan}
        host.status = lambda: state
        seen = {}
        def propose(*args, harness, **kwargs):
            seen["system"] = harness["system_prompt"]
            return Outcome(data={"content": [{"type": "text", "text": "summary of earlier work"}], "usage": {}})
        with mock.patch("kernelcat_executor.operations.requests.propose", propose), \
             mock.patch("kernelcat_executor.contracts.harness.validate_replay", lambda value, _kind: value):
            result = host.compact({"input_id": "c1", "harness": {"system_prompt": "Summarize.", "messages": [], "tools": []}})
        self.assertIn('"plan":', seen["system"], "the summarizer is told to keep the plan")
        self.assertTrue(result["text"].startswith("summary of earlier work"))
        self.assertIn("Current Goal plan (1/2 complete) [x] reproduce; [>] fix.", result["text"])
        state["goal"]["status"] = "completed"
        with mock.patch("kernelcat_executor.operations.requests.propose", propose), \
             mock.patch("kernelcat_executor.contracts.harness.validate_replay", lambda value, _kind: value):
            result = host.compact({"input_id": "c2", "harness": {"system_prompt": "Summarize.", "messages": [], "tools": []}})
        self.assertNotIn("Current Goal plan", result["text"], "an ended Goal's plan is not carried forward")


if __name__ == "__main__":
    unittest.main()
