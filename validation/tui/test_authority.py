"""Application protocol/fault tests. No model service or device is used."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

import httpx
from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, BudgetExceeded, Policy, canonical, Outcome, ToolFailure
from kernelcat_executor.contracts.requests import ModelRequest
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_executor.state.application import ApplicationJournal, ModelAdmissionStopped
from kernelcat_executor.capabilities.streaming import observe, request_stream
from kernelcat_worker.loopx_application import recommend
from kernelcat_worker.application_artifacts import source_snapshot, restore_source


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kb-authority-")
        self.root = Path(self.temp.name).resolve()
        self.store = AgentJournal(self.root / "state/events.jsonl")
        self.store.initialize(20, Policy(max_steps=20))
        self.task = {"id": "task", "scope": "test", "goal": "implement", "contract": {}, "seed_files": {}}
        self.store.register_task(self.task)
        self.app = ApplicationJournal(self.store, "application")
        self.defaults = {"max_trials": 3, "max_forks": 2, "max_model_turns": 4}
        self.app.command("create", {"task": self.task, "evaluator_sha256": "evaluator", "goal_defaults": self.defaults,
            "goal": {**self.defaults, "goal_id": "goal", "status": "active", "on_detach": "continue"}})
        self.lease = self.app.command("acquire", {"worker_id": "worker", "mode": "headless"})["lease"]
        self.store.bind_application_lease("application", "worker", self.lease["lease_epoch"])

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, key="input", text="hello", kind="prompt"):
        return self.app.command("accept", {"input_id": key, "text": text, "kind": kind, "channel": "sdk", "initiator": "user"})

    def control(self, operation, **values):
        self.accept(operation + str(len(self.store.events())), operation, "control")
        key = list(self.app.state()["inputs"])[-1]
        return self.app.command("control", {"input_id": key, "operation": operation, **values})

    def test_duplicate_input_ack_and_conflict(self):
        original = self.accept()
        self.assertEqual(original, self.accept())
        with self.assertRaises(Conflict): self.accept(text="different")
        self.app.command("transition", {**self.lease, "input_id": "input", "status": "applied", "pi_session_id": "pi", "entry_id": "entry"})
        current = self.accept()
        self.assertEqual(current["status"], "applied")
        self.app.command("transition", {**self.lease, "input_id": "input", "status": "completed"})
        with self.assertRaises(Conflict): self.app.command("transition", {**self.lease, "input_id": "input", "status": "queued"})

    def test_simultaneous_claims_and_old_epoch(self):
        self.app.command("release", self.lease)
        results = []
        def claim(name):
            try: results.append(self.app.command("acquire", {"worker_id": name, "mode": "headless"}))
            except Conflict: results.append(None)
        threads = [threading.Thread(target=claim, args=(str(i),)) for i in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(result is not None for result in results), 1)
        with self.assertRaises(Conflict): self.store.admit(Action(id="late", task="task", tool="read", arguments={}), 1, self.store.policy())
        self.assertEqual(self.store.snapshot()["reserved"], 0)

    def test_unresolved_call_fences_takeover_without_spending_again(self):
        self.app.command("call_intent", {**self.lease, "call_id": "call", "method": "tool", "params": {"value": 1}})
        self.app.command("release", self.lease)
        with self.assertRaises(Conflict): self.app.command("acquire", {"worker_id": "next", "mode": "headless"})
        self.assertEqual(self.store.snapshot()["reserved"], 0)

    def test_unstarted_admission_cannot_bypass_lease_fence(self):
        action = Action(id="unstarted", task="task", tool="read", arguments={})
        self.store.admit(action, 1, self.store.policy())
        unbound = AgentJournal(self.store.path)
        with self.assertRaises(Conflict): unbound.started(action)
        self.app.command("release", self.lease)
        new = self.app.command("acquire", {"worker_id": "new", "mode": "headless", "recovering": True})["lease"]
        self.store.bind_application_lease("application", "new", new["lease_epoch"])
        with self.assertRaises(Conflict): self.store.started(action)
        self.assertEqual(self.store.snapshot()["reserved"], 1)
        self.store.cancel_unstarted(action)
        self.assertEqual(self.store.snapshot()["reserved"], 0)
        self.assertEqual(self.store.snapshot()["spent"], 0)

    def test_uncertain_effect_keeps_its_reservation(self):
        action = Action(id="uncertain", task="task", tool="evaluate", arguments={})
        self.store.admit(action, 3, self.store.policy())
        self.store.started(action)
        self.store.settle(action, Outcome(status="unknown", data={"submission_key": action.id}), 0)
        with self.assertRaises(InDoubt): self.store.cancel_unstarted(action)
        self.assertEqual(self.store.snapshot()["reserved"], 3)

    def test_pause_interrupt_and_control_idempotence(self):
        state = self.control("interrupt")
        self.assertEqual(state["goal"]["status"], "awaiting_user")
        self.accept()
        self.app.command("transition", {**self.lease, "input_id": "input", "status": "applied", "pi_session_id": "s", "entry_id": "e"})
        self.assertEqual(self.app.state()["goal"]["status"], "active")
        self.control("pause")
        with self.assertRaises(ModelAdmissionStopped) as stopped:
            self.store.admit(Action(id="model", task="task", tool="__propose__", arguments={}), 1, self.store.policy())
        self.assertEqual(stopped.exception.status, "paused")
        self.assertNotIn("model", self.store.snapshot()["actions"])
        self.assertEqual(self.store.snapshot()["reserved"], 0)
        self.control("resume")
        self.assertEqual(self.app.state()["goal"]["status"], "active")

    def test_budget_counts_compaction_and_fork_does_not_reset(self):
        for index in range(4):
            action = Action(id=f"model-{index}", task="task", tool="__propose__", arguments={})
            self.store.admit(action, 1, self.store.policy())
            self.store.started(action)
            self.store.settle(action, Outcome(data={}), 0)
        self.app.command("trial", {**self.lease, "trial_id": "trial", "source_sha256": "source", "pi_session_id": "s"})
        with self.assertRaises(BudgetExceeded): self.store.admit(Action(id="compact", task="task", tool="__propose__", arguments={}), 1, self.store.policy())
        self.assertEqual(self.store.snapshot()["spent"], 4)

    def report_harness_failure(self, **values):
        return self.app.command("configuration", {**self.lease, "operation": "harness_failure",
            "reason": "harness_failure", "summary": "Invalid ReplayContextBoundary: unsafe integer",
            "goal_id": "goal", "input_ids": ["input"], **values})

    def test_harness_failure_has_original_cause_and_requires_explicit_resume(self):
        self.accept()
        self.app.command("transition", {**self.lease, "input_id": "input", "status": "applied",
            "pi_session_id": "pi", "entry_id": "entry"})
        before_inputs = self.app.state()["inputs"]
        state = self.report_harness_failure()
        self.assertEqual(state["inputs"], before_inputs)  # No fabricated user control/settlement.
        self.assertEqual(state["goal"]["status"], "failed")
        self.assertEqual(state["goal"]["reason"], "harness_failure")
        self.assertIn("ReplayContextBoundary", state["goal"]["summary"])
        row = self.store.events()[-1]
        self.assertEqual([item["kind"] for item in row["events"]], ["application/configuration", "application/goal"])
        with self.assertRaises(ModelAdmissionStopped):
            self.store.admit(Action(id="after-failure", task="task", tool="__propose__", arguments={}), 1, self.store.policy())
        # An explicitly resumed fresh worker with valid history is admitted;
        # this does not claim to repair the failed worker's native history.
        self.control("resume")
        self.assertEqual(self.app.state()["goal"]["status"], "active")
        self.assertNotIn("reason", self.app.state()["goal"])
        self.assertNotIn("ended_at_ns", self.app.state()["goal"])
        self.assertEqual(self.app.state()["configurations"][-1]["reason"], "harness_failure")

    def test_harness_failure_preserves_unknown_effects_and_original_reservations(self):
        action = Action(id="original-unknown", task="task", tool="evaluate", arguments={})
        self.store.admit(action, 3, self.store.policy())
        self.store.started(action)
        self.store.settle(action, Outcome(status="unknown", data={"handle": "original"}), 0)
        self.app.command("call_intent", {**self.lease, "call_id": "original-call", "method": "tool", "params": {}})
        before = self.store.snapshot()
        self.report_harness_failure()
        self.assertEqual(self.store.snapshot()["actions"], before["actions"])
        self.assertEqual(self.store.snapshot()["reserved"], 3)
        self.assertEqual(self.app.state()["calls"]["original-call"]["status"], "pending")
        self.app.command("release", self.lease)
        with self.assertRaises(Conflict):
            self.app.command("acquire", {"worker_id": "replacement", "mode": "headless"})

    def test_late_harness_failure_does_not_replace_user_pause_or_new_goal(self):
        self.control("pause")
        self.assertEqual(self.report_harness_failure()["goal"]["status"], "paused")
        self.control("set", objective="replacement")
        replacement = self.app.state()["goal"]
        self.assertNotEqual(replacement["goal_id"], "goal")
        self.assertEqual(self.report_harness_failure()["goal"], replacement)
        self.app.command("goal_outcome", {"status": "budget_limit", "reason": "model_limit"})
        self.assertEqual(self.report_harness_failure(goal_id=replacement["goal_id"])["goal"]["reason"], "model_limit")

    def test_harness_failure_rejects_invalid_or_stale_reports(self):
        for summary in ("", 17, "界" * 3000):
            before = self.store.events()
            with self.assertRaises(Conflict): self.report_harness_failure(summary=summary)
            self.assertEqual(self.store.events(), before)
        with self.assertRaises(Conflict): self.report_harness_failure(lease_epoch=self.lease["lease_epoch"] + 1)

    def evidence(self, tier, **values):
        return {"evidence_tier": tier, "evidence_ref": "receipt-" + tier, "task_version": 1,
            "source_sha256": "source", "evaluator_sha256": "evaluator", "measurement_profile_version": "v1",
            "correct": True, "full_suite": True, "raw_data_ref": "raw", "score": 100, **values}

    def test_single_score_negative_pair_and_official_are_separate(self):
        self.app.command("evidence", self.evidence("E2"))
        self.assertIsNone(self.app.state()["best"])
        for delta in (-1, 0, 0.1):
            with self.assertRaises(Conflict): self.app.command("evidence", self.evidence("E3", parent_source_sha256="parent", pair_method="ABBA", paired_delta=delta, noise_threshold=0.1))
        self.app.command("evidence", self.evidence("E3", parent_source_sha256="parent", pair_method="ABBA", paired_delta=0.2, noise_threshold=0.1))
        self.app.command("evidence", self.evidence("E4", platform_receipt="official-original", score=90))
        self.assertEqual(self.app.state()["best"]["evidence_tier"], "E3")
        self.assertEqual(self.app.state()["platform"][0]["score"], 90)
        self.app.command("evidence", self.evidence("E3", evidence_ref="other-branch", source_sha256="other-source",
            parent_source_sha256="old-parent", pair_method="ABBA", paired_delta=0.2, noise_threshold=0.1))
        self.assertEqual(self.app.state()["best"]["source_sha256"], "source")
        with self.assertRaises(Conflict): self.app.command("evidence", self.evidence("E1", evaluator_sha256="other"))
        self.control("set", objective="new objective")
        self.assertIsNone(self.app.state()["best"])
        with self.assertRaises(Conflict): self.app.command("evidence", self.evidence("E1"))

    def test_unknown_measurements_never_mean_stagnation(self):
        state = self.app.state() | {"budget": {"limit": 20, "spent": 0, "reserved": 0},
            "pending_effect_ids": [], "model_turns": 1, "max_trial_turns": 20}
        self.assertEqual(recommend(state)["action"], "continue")
        state["evidence"] = [self.evidence("E2", evidence_ref=str(i), noise_threshold=1) for i in range(3)]
        state["model_turns"] = 3
        self.assertEqual(recommend(state)["action"], "reset")
        state["evidence"][-1]["measurement_profile_version"] = "v2"
        self.assertEqual(recommend(state)["action"], "continue")

    def test_acceptance_changes_create_a_new_task_version(self):
        self.control("set", objective=self.task["goal"], acceptance={"evidence_tier": "E3", "target": 1.5})
        state = self.app.state()
        self.assertEqual(state["task_version"], 2)
        self.assertEqual(state["task"]["contract"]["acceptance"], {"evidence_tier": "E3", "target": 1.5})
        with self.assertRaises(Conflict): self.app.command("evidence", self.evidence("E1"))

    def test_source_rollback_never_changes_reference(self):
        root = self.root / "workspace"
        (root / "reference").mkdir(parents=True)
        (root / "reference/spec").write_text("immutable")
        (root / "solution.py").write_text("one")
        snapshot = source_snapshot(root, ["solution.py"], ["reference"])
        (root / "solution.py").write_text("two")
        restore_source(root, snapshot, ["solution.py"], ["reference"])
        self.assertEqual((root / "solution.py").read_text(), "one")
        self.assertEqual((root / "reference/spec").read_text(), "immutable")


class StreamingTests(unittest.TestCase):
    def request(self, chunks, *, secret="", finish=True):
        raw = "".join("data: " + json.dumps(item) + "\n\n" for item in chunks)
        if finish: raw += "data: [DONE]\n\n"
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=raw.encode()))
        request = ModelRequest(provider="openai", endpoint="https://example.test/chat/completions", body_json=canonical({"stream": True}))
        deltas = []
        with httpx.Client(transport=transport) as client, observe(deltas.append):
            result = request_stream(request, {}, provider="openai", max_bytes=65536, client=client, secret=secret)
        return result, deltas

    def test_real_chunks_and_completed_tool_arguments(self):
        chunks = [{"choices": [{"index": 0, "delta": {"content": text}}]} for text in ["中", "文 output"]]
        chunks += [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call", "function": {"name": "read", "arguments": '{"path":'}}]}}]},
                   {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]}, "finish_reason": "tool_calls"}]}]
        result, deltas = self.request(chunks)
        self.assertEqual("".join(item["delta"] for item in deltas), "中文 output")
        self.assertEqual(json.loads(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]), {"path": "a"})

    def test_truncated_stream_and_split_credentials_fail_closed(self):
        with self.assertRaises(ToolFailure): self.request([{"choices": [{"delta": {"content": "partial"}}]}], finish=False)
        with self.assertRaises(ToolFailure): self.request([{"choices": [{"delta": {"content": text}}]} for text in ["secret", "-credential"]], secret="secret-credential")
        with self.assertRaises(ToolFailure): self.request([{"choices": [{"delta": {"content": text}}]} for text in ["ab s", "ecr", "et-credential tail"]], secret="secret-credential")

    def test_reasoning_streams_as_thinking_and_is_kept_apart_from_the_text(self):
        chunks = [{"choices": [{"delta": {"reasoning_content": "先检查"}}]}, {"choices": [{"delta": {"reasoning_content": "输入"}}]},
                  {"choices": [{"delta": {"content": "结论"}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        result, deltas = self.request(chunks)
        self.assertEqual([(d["type"], d["delta"]) for d in deltas], [("thinking_delta", "先检查"), ("thinking_delta", "输入"), ("text_delta", "结论")])
        self.assertEqual(result["choices"][0]["message"]["content"], "结论")
        # Thinking mode with tool calls needs the reasoning back on the next request.
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"], "先检查输入")

    def test_stream_holds_only_a_possible_credential_prefix(self):
        secret = "sk-0123456789abcdefghijklmnopqrstuvw"
        pieces = ["第一句话已经结束。", "第二句", "话 sk-01", "2 不是密钥", "。"]
        chunks = [{"choices": [{"delta": {"content": text}}]} for text in pieces]
        _, deltas = self.request(chunks + [{"choices": [{"delta": {}, "finish_reason": "stop"}]}], secret=secret)
        emitted = "".join(item["delta"] for item in deltas)
        self.assertEqual(emitted, "".join(pieces))
        # Ordinary prose is forwarded with the frame that carried it; only the
        # possible credential prefix "sk-01" waits for the next frame.
        self.assertEqual(deltas[0]["delta"], "第一句话已经结束。")
        self.assertEqual(deltas[1]["delta"], "第二句")
        self.assertEqual(deltas[2]["delta"], "话 ")
        self.assertTrue(all(secret not in emitted[:end] for end in range(len(emitted) + 1)))


if __name__ == "__main__":
    unittest.main()
