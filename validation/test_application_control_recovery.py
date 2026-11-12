"""Consistent host snapshots and recovery of a deliberately lost pause reply.

Only temporary Journals and a local socketpair are used. No original session,
provider or device is contacted, and no latency threshold is an acceptance gate.
"""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import Action, Outcome, Policy
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_executor.state.application import ApplicationJournal, ModelAdmissionStopped
from kernelcat_worker.application_host import ApplicationHost, Connection


class ApplicationControlRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kb-control-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.policy = Policy()
        self.store = AgentJournal(self.root / "events.jsonl")
        self.store.initialize(None, self.policy)
        task = {"id": "task", "goal": "fixture", "scope": "code", "contract": {}}
        self.store.register_task(task)
        self.app = ApplicationJournal(self.store, "application")
        self.app.command("create", {"task": task, "workspace_mode": "isolated",
            "goal_defaults": {}, "goal": {"goal_id": "goal", "status": "active",
            "objective": "fixture", "on_detach": "continue", "max_trials": None,
            "max_model_turns": None, "token_budget": None, "max_forks": None}})
        self.lease = self.app.command("acquire", {"worker_id": "worker", "mode": "headless"})["lease"]
        self.store.bind_application_lease("application", "worker", self.lease["lease_epoch"])

        # Keep the real request/status/connection implementations; only the
        # unrelated owner configuration and provider catalogue are fixture data.
        self.host = ApplicationHost.__new__(ApplicationHost)
        self.host.identity = "application"
        self.host.journal = self.app
        self.host.config = SimpleNamespace(workspace_mode="isolated", policy=self.policy,
            subagents=SimpleNamespace(model_dump=lambda: {}),
            providers={"primary": SimpleNamespace(model="local-fixture")})
        self.host.owner = SimpleNamespace(gateway=SimpleNamespace(store=self.store, tools={}),
                                         workspace_policy={"root": str(self.root)})
        self.host.descriptor = {"goal_defaults": {}}
        self.host.token = "local-fixture-token"
        self.host.clients = set()
        self.host.mutex = threading.RLock()

    def start(self, name, tool, credits):
        action = Action(id=name, task="task", tool=tool)
        self.store.admit(action, credits, self.policy)
        self.store.started(action)
        return action

    def test_status_uses_one_snapshot_even_when_original_model_settles_after_read(self):
        first = self.start("first-model", "__propose__", 1)
        self.store.settle(first, Outcome(data={"usage": {"total_tokens": 17}}), 0)
        pending = self.start("already-started-model", "__propose__", 2)
        self.host.request(None, {"method": "control", "params": {
            "input_id": "original-pause", "operation": "pause", "channel": "sdk"}})
        expected_cursor = len(self.store.events()) - 1
        before = self.store.snapshot()

        writer = AgentJournal(self.store.path)
        writer.bind_application_lease("application", "worker", self.lease["lease_epoch"])
        original_transaction = self.store._transaction
        settled = False

        def read_then_settle(fn, *args, **kwargs):
            nonlocal settled
            result = original_transaction(fn, *args, **kwargs)
            if not settled:
                settled = True
                # A real independent writer advances the original action only
                # after the status snapshot's lock was released.
                writer.settle(pending, Outcome(data={"usage": {"total_tokens": 23}}), 0)
            return result

        with mock.patch.object(self.store, "_transaction", side_effect=read_then_settle) as transactions:
            status = self.host.status()
        self.assertEqual(transactions.call_count, 1)
        self.assertTrue(settled)
        self.assertEqual(status["cursor"], expected_cursor)
        self.assertEqual(status["goal"]["status"], "paused")
        self.assertEqual(status["model_turns"], 2)
        self.assertEqual(status["goal_usage"]["tokens"], 17)
        self.assertEqual(status["budget"]["reserved"], before["reserved"])
        self.assertEqual(status["pending_effect_ids"], [pending.id])
        # The next consistent snapshot sees all of the later settlement.
        latest = self.host.status()
        self.assertGreater(latest["cursor"], expected_cursor)
        self.assertEqual(latest["goal_usage"]["tokens"], 40)
        self.assertEqual(latest["budget"]["reserved"], 0)
        self.assertEqual(latest["pending_effect_ids"], [])

    def test_lost_pause_reply_is_recovered_by_query_without_resubmission(self):
        original = self.start("original-tool", "read_file", 3)
        local, peer = socket.socketpair()
        peer.settimeout(10)
        connection = Connection(self.host, local)
        reader = peer.makefile("rb")
        dropped = threading.Event()
        lost_replies = []
        emit = connection.emit

        def lose_only_pause_reply(message):
            if message.get("id") == 2:
                lost_replies.append(message)
                dropped.set()
                return
            emit(message)

        connection.emit = lose_only_pause_reply
        thread = threading.Thread(target=connection.run, daemon=True)
        thread.start()

        def send(identity, method, params=None):
            peer.sendall((json.dumps({"id": identity, "method": method, "params": params or {}}) + "\n").encode())

        def receive(identity):
            reply = json.loads(reader.readline())
            self.assertEqual(reply["id"], identity)
            self.assertNotIn("error", reply)
            return reply["result"]

        try:
            send(1, "authenticate", {"token": self.host.token})
            self.assertEqual(receive(1)["application_session_id"], "application")
            with mock.patch.object(self.host, "request", wraps=self.host.request) as requests:
                send(2, "control", {"input_id": "original-pause", "operation": "pause", "channel": "sdk"})
                self.assertTrue(dropped.wait(10), "original pause handler did not reach its reply")
                self.assertEqual(len(lost_replies), 1)
                self.assertNotIn("error", lost_replies[0])
                # A lost ACK is not proof of failure. Query this same session;
                # do not submit pause again, with either the old or a new ID.
                send(3, "status")
                status = receive(3)
                self.assertEqual(status["goal"]["status"], "paused")
                receipt = status["inputs"]["original-pause"]
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(receipt["control_result"]["status"], "paused")
                self.assertEqual(status["pending_effect_ids"], [original.id])

                before_rejected_model = self.store.path.read_bytes()
                with self.assertRaises(ModelAdmissionStopped) as denied:
                    self.store.admit(Action(id="must-not-start", task="task", tool="__propose__"), 5, self.policy)
                self.assertEqual(str(denied.exception), "model admission stopped by application state: paused")
                self.assertEqual(self.store.path.read_bytes(), before_rejected_model)
                self.assertEqual(self.store.snapshot()["reserved"], 3)
                # Pause does not falsify or cancel the original admitted effect.
                result = Outcome(data={"original": "finished"})
                self.store.settle(original, result, 0)
                send(4, "status")
                final = receive(4)
                self.assertEqual(final["goal"]["status"], "paused")
                self.assertEqual(final["pending_effect_ids"], [])
                self.assertEqual(final["model_turns"], 0)
                self.assertEqual(final["budget"]["reserved"], 0)
                self.assertEqual(final["budget"]["spent"], 3)
                self.assertEqual([call.args[1]["method"] for call in requests.call_args_list],
                                 ["control", "status", "status"])

            rows = self.store.events()
            events = list(self.store._events(rows))
            accepted = [event for event in events if event["kind"] == "application/input_accepted"
                        and event["data"]["input_id"] == "original-pause"]
            completed = [event for event in events if event["kind"] == "application/input_transition"
                         and event["data"]["input_id"] == "original-pause"]
            self.assertEqual(len(accepted), 1)
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["data"]["status"], "completed")
            atomic = next(row for row in rows if row["seq"] == completed[0]["seq"])
            self.assertEqual(atomic["kind"], "atomic")
            self.assertEqual([event["kind"] for event in atomic["events"]],
                             ["application/goal", "application/input_transition"])
            settled_original = self.store.snapshot()["actions"][original.id]
            self.assertEqual(settled_original["action"], original.model_dump(mode="json"))
            self.assertEqual(settled_original["outcome"], result.model_dump(mode="json"))
        finally:
            connection.close()
            reader.close()
            peer.close()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "local fixture connection failed to stop")


if __name__ == "__main__":
    unittest.main()
