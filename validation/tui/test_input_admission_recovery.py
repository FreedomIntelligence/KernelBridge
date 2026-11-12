"""Real Journal admission boundaries and recovery after a lost enqueue ACK.

No provider, device, original session or network is used.
"""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.agent import AgentOptions
from kernelcat_executor.contracts.models import Conflict, Policy
from kernelcat_executor.operations.control import SessionControl
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_executor.state.application import ApplicationJournal
from kernelcat_worker.application_host import ApplicationHost


class InputAdmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="kb-input-admission-")
        self.addCleanup(temp.cleanup)
        self.store = AgentJournal(Path(temp.name) / "events.jsonl")
        self.store.initialize(None, Policy())
        self.task = {"id": "task", "goal": "fixture", "scope": "code", "contract": {}}
        self.store.register_task(self.task)
        self.app = ApplicationJournal(self.store, "application")
        self.app.command("create", {"task": self.task, "workspace_mode": "task", "goal_defaults": {},
            "goal": {"goal_id": "goal", "status": "active", "objective": "complete original fixture",
                     "on_detach": "continue", "max_trials": None, "max_model_turns": None,
                     "token_budget": None, "max_forks": None}})
        lease = self.app.command("acquire", {"worker_id": "worker", "mode": "headless"})["lease"]
        self.store.bind_application_lease("application", "worker", lease["lease_epoch"])
        self.connection = SimpleNamespace(worker=lease)
        self.gateway = Gateway(self.store, [], "code")
        self.control = SessionControl(self.gateway, AgentOptions())
        self.enqueues = []

        def enqueue(task, text, kind, key):
            self.enqueues.append(key)
            return self.control.enqueue(task["id"], text, kind, key)

        self.host = ApplicationHost.__new__(ApplicationHost)
        self.host.journal = self.app
        self.host.owner = SimpleNamespace(gateway=self.gateway, enqueue_input=enqueue)
        self.host.clients = set()
        self.host.mutex = threading.RLock()

    def accept(self, text, identity="original", **extra):
        return self.app.command("accept", {"input_id": identity, "text": text, "kind": "policy",
            "initiator": "loopx", "channel": "policy", **extra})

    def test_deterministic_legacy_rejection_is_durable_and_does_not_retry(self):
        # Same old representation: the Goal ID was embedded only in the final
        # JSON line, with enough Unicode seed data to exceed the executor limit.
        text = "原始不可截断的指令" * 4000 + "\n" + json.dumps({"goal": {"goal_id": "goal"}})
        self.accept(text)
        result = self.host.enqueue_input(self.connection, self.task, "original")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["input_bytes"], len(text.encode("utf-8")))
        self.assertIn("64 KiB", result["error_summary"])
        current = self.app.state()
        self.assertEqual(current["inputs"]["original"]["status"], "failed")
        self.assertEqual(current["inputs"]["original"]["text"], text)
        self.assertEqual(current["goal"]["status"], "awaiting_user")
        self.assertEqual(self.enqueues, [])
        before = self.store.events()
        repeated = self.host.enqueue_input(self.connection, self.task, "original")
        self.assertEqual(repeated, result)
        self.assertEqual(self.store.events(), before)

    def test_rejecting_old_goal_input_does_not_change_current_goal(self):
        self.accept("x" * 65537, goal_id="another-old-goal")
        result = self.host.enqueue_input(self.connection, self.task, "original")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.app.state()["goal"]["status"], "active")
        self.assertEqual(self.app.state()["goal"]["goal_id"], "goal")

    def test_control_arriving_between_rejection_and_goal_outcome_wins(self):
        self.accept("x" * 65537, goal_id="goal")
        command = self.app.command
        raced = []

        def concurrent_pause(method, data):
            if method == "goal_outcome" and not raced:
                raced.append(True)
                command("accept", {"input_id": "user-pause", "text": "pause", "kind": "control",
                                   "initiator": "user", "channel": "tui"})
                command("control", {"input_id": "user-pause", "operation": "pause"})
            return command(method, data)

        self.app.command = concurrent_pause
        result = self.host.enqueue_input(self.connection, self.task, "original")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.app.state()["inputs"]["original"]["status"], "failed")
        self.assertEqual(self.app.state()["goal"]["status"], "paused")
        self.assertNotIn("expected_goal_sha256", self.app.state()["goal"])

    def test_unknown_ack_preserves_same_id_and_one_executor_receipt(self):
        self.accept("continue exact original objective", goal_id="goal")
        original_enqueue = self.host.owner.enqueue_input
        lost = True

        def enqueue_with_lost_ack(*args):
            nonlocal lost
            identity = original_enqueue(*args)
            if lost:
                lost = False
                raise ConnectionError("fixture dropped acknowledgement after durable executor admission")
            return identity

        self.host.owner.enqueue_input = enqueue_with_lost_ack
        with self.assertRaises(ConnectionError):
            self.host.enqueue_input(self.connection, self.task, "original")
        self.assertEqual(self.app.state()["inputs"]["original"]["status"], "queued")
        self.assertEqual(self.app.state()["goal"]["status"], "active")
        reply = self.host.enqueue_input(self.connection, self.task, "original")
        self.assertEqual(reply["status"], "completed")
        inputs = [row for row in self.store.events()
                  if row["kind"] == "admitted" and row["action"]["tool"] == "__input__"]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["action"]["id"], reply["executor_input_id"])
        self.assertEqual(self.enqueues, ["original", "original"])
        self.assertEqual(len(self.control.pending("task")), 1)

    def test_uncertain_failure_before_admission_is_not_guessed_to_be_rejected(self):
        self.accept("continue", goal_id="goal")
        def unavailable(*args):
            raise TimeoutError("unknown executor admission")
        self.host.owner.enqueue_input = unavailable
        with self.assertRaises(TimeoutError):
            self.host.enqueue_input(self.connection, self.task, "original")
        self.assertEqual(self.app.state()["inputs"]["original"]["status"], "queued")
        self.assertEqual(self.app.state()["goal"]["status"], "active")

    def test_host_accept_validates_byte_limit_before_any_durable_receipt(self):
        for index, text in enumerate(("x" * 65537, "中" * 21846, "  ")):
            with self.subTest(index=index):
                before = self.store.events()
                with self.assertRaisesRegex(Conflict, "before executor admission"):
                    self.host.request(None, {"method": "accept", "params": {
                        "input_id": f"invalid-{index}", "text": text, "kind": "prompt",
                        "initiator": "user", "channel": "tui"}})
                self.assertEqual(self.store.events(), before)
        for index, text in enumerate(("x" * 65536, "中" * 21845 + "x")):
            with self.subTest(valid=index):
                result = self.host.request(None, {"method": "accept", "params": {
                    "input_id": f"valid-{index}", "text": text, "kind": "prompt",
                    "initiator": "user", "channel": "tui"}})
                self.assertEqual(result["status"], "queued")
                self.assertEqual(self.app.state()["inputs"][result["input_id"]]["text"], text)

    def test_interrupted_receipt_cannot_be_reenqueued(self):
        self.accept("continue", goal_id="goal")
        self.app.command("transition", {**self.connection.worker, "input_id": "original",
                                       "status": "interrupted", "reason": "fixture"})
        with self.assertRaisesRegex(Conflict, "no longer queued"):
            self.host.enqueue_input(self.connection, self.task, "original")
        self.assertEqual(self.enqueues, [])


if __name__ == "__main__":
    unittest.main()
