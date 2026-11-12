"""Timeouts, retries and idempotency, aligned with Claude Code and Codex: bounded
automatic retry of transient provider failures as separate journaled attempts, never
of side-effecting tools; a subagent deadline that ends with a summary instead of
nothing; and an active Goal that waits for the user during a provider outage.
No model service or device is used."""
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

from kernelcat_executor.capabilities.http import _retry_after, _status_failure
from kernelcat_executor.capabilities.streaming import observe, request_stream
from kernelcat_executor.contracts.models import Policy, ToolFailure, canonical
from kernelcat_executor.contracts.requests import ModelRequest
from kernelcat_executor.operations.recovery import _successor, model_chain_length
from kernelcat_executor.operations.retry_policy import failure_summary, retry_delay, retryable


class PolicyTests(unittest.TestCase):
    def test_only_failures_before_any_output_are_retryable(self):
        self.assertTrue(retryable({"category": "rate_limit"}))
        self.assertTrue(retryable({"category": "server", "http_status": 529}))
        self.assertTrue(retryable({"category": "timeout", "output_started": False}))
        self.assertTrue(retryable({"category": "transport", "output_started": False}))
        for details in ({"category": "timeout", "output_started": True}, {"category": "transport"},
                        {"category": "authentication"}, {"category": "http", "http_status": 400},
                        {"category": "incomplete_output"}, {"category": "protocol"}, {}, None):
            self.assertFalse(retryable(details), details)

    def test_capped_exponential_backoff_with_jitter(self):
        details = {"category": "server"}
        self.assertEqual([retry_delay(details, attempt, 10, rng=lambda: 0.0) for attempt in range(6)],
                         [0.5, 1.0, 2.0, 4.0, 8.0, 10.0])
        self.assertEqual([retry_delay(details, attempt, 10, rng=lambda: 1.0) for attempt in range(6)],
                         [1.0, 2.0, 4.0, 8.0, 16.0, 20.0])
        self.assertIsNone(retry_delay(details, 4, 4), "at most max_retries")
        self.assertIsNone(retry_delay(details, 0, 0), "0 turns retries off")

    def test_retry_after_is_a_capped_floor(self):
        self.assertEqual(retry_delay({"category": "rate_limit", "retry_after": 7}, 0, 4, rng=lambda: 0.0), 7.0)
        self.assertEqual(retry_delay({"category": "rate_limit", "retry_after": 600}, 0, 4, rng=lambda: 0.0), 60.0)
        self.assertEqual(retry_delay({"category": "rate_limit", "retry_after": 0.1}, 2, 4, rng=lambda: 0.0), 2.0)

    def test_failure_summary_never_carries_a_body(self):
        self.assertEqual(failure_summary({"category": "server", "http_status": 503, "body": "secret"}),
                         {"category": "server", "http_status": 503})


class TransportTests(unittest.TestCase):
    def test_status_failure_records_retry_after(self):
        details = _status_failure(429, "12").details
        self.assertEqual((details["category"], details["retry_after"], details["output_started"]), ("rate_limit", 12.0, False))
        self.assertNotIn("retry_after", _status_failure(503, "soon").details)
        self.assertEqual(_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0, "a past HTTP date means now")

    def stream(self, handler):
        transport = httpx.MockTransport(handler)
        request = ModelRequest(provider="openai", endpoint="https://example.test/chat/completions", body_json=canonical({"stream": True}))
        with httpx.Client(transport=transport) as client, observe(lambda _delta: None):
            return request_stream(request, {}, provider="openai", max_bytes=65536, client=client)

    def test_stream_failures_say_whether_output_had_started(self):
        class Broken(httpx.SyncByteStream):
            def __init__(self, events): self.events = events
            def __iter__(self):
                yield from self.events
                raise httpx.ReadTimeout("stalled")
        event = b'data: {"choices": [{"index": 0, "delta": {"content": "partial"}}]}\n\n'
        with self.assertRaises(ToolFailure) as after:
            self.stream(lambda _request: httpx.Response(200, stream=Broken([event])))
        self.assertEqual((after.exception.details["category"], after.exception.details["output_started"]), ("timeout", True))
        with self.assertRaises(ToolFailure) as before:
            self.stream(lambda _request: httpx.Response(200, stream=Broken([])))
        self.assertIs(before.exception.details["output_started"], False)
        with self.assertRaises(ToolFailure) as status:
            self.stream(lambda _request: httpx.Response(503, headers={"retry-after": "3"}))
        self.assertEqual((status.exception.details["category"], status.exception.details["retry_after"]), ("server", 3.0))


class AuthorityRetryTests(unittest.TestCase):
    """The parent model step permits one more attempt on the same route, on the
    deterministic successor chain, at most max_retries times in total."""

    def rpc(self, actions, max_retries=2):
        from kernelcat_executor.operations.authority import ExecutionRPC
        rpc = object.__new__(ExecutionRPC)
        rpc.task, rpc.step, rpc.route_index, rpc.retry = SimpleNamespace(id="t"), 3, 1, None
        rpc.store = SimpleNamespace(snapshot=lambda: {"actions": actions})
        rpc.owner = SimpleNamespace(proposers={"ds": SimpleNamespace(max_retries=max_retries)})
        return rpc

    def test_chain_bounds_the_retries(self):
        base = "t/step/3/model/ds"
        first = _successor(base)
        actions = {base: {}, first: {}}
        self.assertEqual(model_chain_length({"actions": actions}, base), 2)
        rpc = self.rpc(actions)
        retry = rpc._automatic_retry(first, "ds", {"category": "server", "http_status": 503})
        self.assertEqual((retry["route"], retry["attempt"], retry["max_retries"], retry["http_status"]), ("ds", 2, 2, 503))
        self.assertEqual((rpc.route_index, rpc.retry.failed_id, rpc.retry.next_id), (0, first, _successor(first)))
        # The chain already holds three attempts: no further retry, and the route advances.
        actions[_successor(first)] = {}
        rpc = self.rpc(actions)
        self.assertIsNone(rpc._automatic_retry(_successor(first), "ds", {"category": "server"}))
        self.assertEqual(rpc.route_index, 1)

    def test_non_transient_failures_are_not_retried(self):
        rpc = self.rpc({"t/step/3/model/ds": {}})
        self.assertIsNone(rpc._automatic_retry("t/step/3/model/ds", "ds", {"category": "http", "http_status": 400}))
        self.assertIsNone(self.rpc({"t/step/3/model/ds": {}}, max_retries=0)._automatic_retry("t/step/3/model/ds", "ds", {"category": "server"}))


class SubagentTimeoutTests(unittest.TestCase):
    def child(self, timeout=100.0, mode="read"):
        from kernelcat_executor.operations.delegated import DelegatedEffects
        child = object.__new__(DelegatedEffects)
        child.stop_event, child.failure, child.stop_reason = SimpleNamespace(is_set=lambda: False), None, None
        child.owner = SimpleNamespace(_cancelled=lambda: False, stopped_children=set())
        child.action = SimpleNamespace(id="d", arguments={})
        child.mode, child.receipt = mode, "r"
        child.started_at = time.monotonic() - timeout
        child.deadline = time.monotonic() - 0.01
        child.timeout_grace = min(60.0, max(10.0, 0.1 * timeout))
        child.timeout_wrap = False
        return child

    def test_deadline_grants_one_final_summary_then_stops(self):
        child = self.child()
        self.assertTrue(child.cancelled(), "running commands are stopped at the deadline")
        self.assertIsNone(child._check(), "the first check after the deadline allows the summary call")
        self.assertTrue(child.timeout_wrap)
        self.assertFalse(child.cancelled(), "within the grace the summary call is not cancelled")
        self.assertGreater(child.deadline, time.monotonic() + 9)
        child.deadline = time.monotonic() - 0.01
        self.assertEqual(child._check()["reason"], "timeout")

    def test_analysis_children_have_no_summary_grace(self):
        self.assertEqual(self.child(mode="analysis")._check()["reason"], "timeout")


class GoalOutageTests(unittest.TestCase):
    def test_an_active_goal_waits_for_the_user_after_every_route_failed(self):
        from test_goal_continuation import state
        from kernelcat_worker.application_host import ApplicationHost
        host = object.__new__(ApplicationHost)
        host.commands = []
        host.journal = SimpleNamespace(command=lambda kind, data: host.commands.append((kind, data)) or {})
        host.config = SimpleNamespace(workspace_mode="project", goal_review=SimpleNamespace(enabled=False))
        failed = {"input_id": "loopx-1", "kind": "policy", "status": "failed", "accepted_seq": 10, "failed_seq": 20,
                  "reason": "providers_failed", "summary": "模型调用失败：ds HTTP 503 服务端错误。"}
        host.status = lambda: state(inputs={"loopx-1": failed})
        self.assertEqual(host.next_input(SimpleNamespace(worker={"worker_id": "w"})), {"action": "wait", "reason": "provider_unavailable"})
        kind, data = host.commands[0]
        self.assertEqual((kind, data["status"], data["reason"]), ("goal_outcome", "awaiting_user", "provider_unavailable"))
        self.assertIn("HTTP 503", data["summary"])
        # A failure for another reason does not pause the Goal here.
        host.commands.clear()
        host.status = lambda: state(inputs={"loopx-1": {**failed, "reason": "harness_failure"}})
        self.assertEqual(host.next_input(SimpleNamespace(worker={"worker_id": "w"}))["action"], "continue")


if __name__ == "__main__":
    unittest.main()
