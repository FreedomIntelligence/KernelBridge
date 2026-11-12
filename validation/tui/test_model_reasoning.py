"""Model and reasoning selection (/model, /effort): the provider contract, the request
fields each level produces, the host's configure handler, and restoring the choice
after a restart. No model service or device is used."""
from pathlib import Path
import json
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

from pydantic import ValidationError

from kernelcat_executor.capabilities.llm import APIProposer
from kernelcat_executor.contracts.models import Conflict, Policy
from kernelcat_executor.contracts.providers import WireOptions

KEY = "k" * 24
DEEPSEEK = "https://api.deepseek.com"


def proposer(base=DEEPSEEK, **options):
    return APIProposer("openai", base, "deepseek-flash", KEY,
                       wire_options=WireOptions(native_turns=True, tool_choice="auto", **options))


def harness():
    return {"schema_version": "kernelcat.pi-harness/v1", "adapter": "pi@1.5.0", "system_prompt": "s",
            "messages": [{"role": "user", "content": "hi"}], "tools": []}


def body(item):
    return json.loads(item.prepare_harness(harness()).body_json)


class ContractTests(unittest.TestCase):
    def test_thinking_can_be_enabled_with_an_effort(self):
        options = WireOptions(thinking="enabled", reasoning_effort="high")
        self.assertEqual((options.thinking, options.reasoning_effort), ("enabled", "high"))

    def test_levels_are_a_fixed_vocabulary_without_repeats(self):
        with self.assertRaises(ValidationError):
            WireOptions(reasoning_levels=("low", "low"))
        with self.assertRaises(ValidationError):
            WireOptions(reasoning_levels=("ultra",))

    def test_anthropic_transport_refuses_thinking_controls(self):
        with self.assertRaisesRegex(ValueError, "OpenAI-compatible"):
            APIProposer("anthropic", "https://api.anthropic.com", "m", KEY,
                        wire_options=WireOptions(reasoning_levels=("low",)))


class RequestFieldTests(unittest.TestCase):
    def test_deepseek_endpoint_offers_its_documented_levels(self):
        item = proposer(thinking="disabled")
        self.assertEqual(item.reasoning_levels(), ("off", "low", "high", "max"))
        self.assertEqual(item.configured_reasoning(), "off")
        self.assertEqual(body(item)["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body(item))

    def test_each_level_sets_thinking_and_effort(self):
        item = proposer(thinking="disabled")
        for level in ("low", "high", "max"):
            item.select_reasoning(level)
            self.assertEqual((body(item)["thinking"], body(item)["reasoning_effort"]), ({"type": "enabled"}, level))
        item.select_reasoning("off")
        self.assertEqual(body(item)["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body(item))
        item.select_reasoning(None)  # auto: back to the configured options
        self.assertEqual(body(item)["thinking"], {"type": "disabled"})

    def test_a_level_the_route_does_not_offer_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not offered"):
            proposer().select_reasoning("xhigh")

    def test_other_endpoints_offer_only_declared_levels(self):
        self.assertEqual(proposer("http://127.0.0.1:9/v1").reasoning_levels(), ())
        declared = proposer("http://127.0.0.1:9/v1", reasoning_levels=("low", "medium", "high"))
        declared.select_reasoning("medium")
        # No "off" level: thinking is not a toggle here, so only the effort is sent.
        self.assertEqual(body(declared).get("reasoning_effort"), "medium")
        self.assertNotIn("thinking", body(declared))
        # An empty declaration turns runtime selection off, even on DeepSeek.
        self.assertEqual(proposer(reasoning_levels=()).reasoning_levels(), ())

    def test_configured_effort_is_sent_when_nothing_is_selected(self):
        item = proposer(thinking="enabled", reasoning_effort="max")
        self.assertEqual(item.configured_reasoning(), "max")
        self.assertEqual((body(item)["thinking"], body(item)["reasoning_effort"]), ({"type": "enabled"}, "max"))

    def test_child_requests_use_the_same_selection(self):
        item = proposer(thinking="disabled")
        item.select_reasoning("high")
        request = json.loads(item.prepare({"policy": {}}, ()).body_json)
        self.assertEqual((request["thinking"], request["reasoning_effort"]), ({"type": "enabled"}, "high"))


class HostConfigureTests(unittest.TestCase):
    def host(self, pending=()):
        from kernelcat_worker.application_host import ApplicationHost
        host = object.__new__(ApplicationHost)
        routes = {"ds": proposer(thinking="disabled"), "ds-pro": proposer(thinking="disabled")}
        children = {"ds": proposer(thinking="disabled"), "ds-pro": proposer(thinking="disabled")}
        policy = {"value": Policy(routes=("ds", "ds-pro"))}

        def install(_expected, selected, _report):
            policy["value"] = selected
        store = SimpleNamespace(policy=lambda: policy["value"], install=install)
        host.owner = SimpleNamespace(proposers=routes, child_proposers=children, gateway=SimpleNamespace(store=store))
        host.config = SimpleNamespace(policy=SimpleNamespace(routes=("ds", "ds-pro")),
                                      providers={"ds": SimpleNamespace(model="deepseek-flash"),
                                                 "ds-pro": SimpleNamespace(model="deepseek-v4-pro")})
        host.rows = []
        host.journal = SimpleNamespace(command=lambda kind, data: host.rows.append((kind, data)))
        host.status = lambda: {"pending_effect_ids": list(pending)}
        return host

    connection = SimpleNamespace(worker={"worker_id": "w"})

    def test_query_lists_routes_levels_and_the_current_one(self):
        info = self.host().configure(self.connection, {"kind": "model"})
        self.assertEqual(info["current"], "ds")
        self.assertEqual([(item["route"], item["model"], item["level"], item["current"]) for item in info["routes"]],
                         [("ds", "deepseek-flash", "off", True), ("ds-pro", "deepseek-v4-pro", "off", False)])
        self.assertEqual(info["routes"][0]["levels"], ["off", "low", "high", "max"])

    def test_route_and_level_together_apply_to_parent_and_child_and_are_recorded(self):
        host = self.host()
        result = host.configure(self.connection, {"kind": "model", "value": "ds-pro", "reasoning": "high"})
        self.assertEqual(result["current"], "ds-pro")
        self.assertEqual(host.owner.gateway.store.policy().routes[0], "ds-pro")
        self.assertEqual(host.owner.proposers["ds-pro"].reasoning, "high")
        self.assertEqual(host.owner.child_proposers["ds-pro"].reasoning, "high")
        self.assertIsNone(host.owner.proposers["ds"].reasoning)
        self.assertEqual([data for _, data in host.rows], [
            {"worker_id": "w", "kind": "model", "value": "ds-pro"},
            {"worker_id": "w", "kind": "reasoning", "route": "ds-pro", "value": "high"}])

    def test_effort_steps_and_auto(self):
        host = self.host()
        self.assertEqual(host.configure(self.connection, {"kind": "reasoning", "value": "up"})["routes"][0]["level"], "low")
        self.assertEqual(host.configure(self.connection, {"kind": "reasoning", "value": "up"})["routes"][0]["level"], "high")
        self.assertEqual(host.configure(self.connection, {"kind": "reasoning", "value": "max"})["routes"][0]["level"], "max")
        self.assertEqual(host.configure(self.connection, {"kind": "reasoning", "value": "up"})["routes"][0]["level"], "max")
        self.assertEqual(host.configure(self.connection, {"kind": "reasoning", "value": "down"})["routes"][0]["level"], "high")
        self.assertEqual(host.configure(self.connection, {"kind": "thinking", "value": "auto"})["routes"][0]["level"], "off")
        self.assertIsNone(host.owner.proposers["ds"].reasoning)

    def test_refusals(self):
        host = self.host()
        with self.assertRaisesRegex(Conflict, "not offered"):
            host.configure(self.connection, {"kind": "reasoning", "value": "xhigh"})
        with self.assertRaisesRegex(Conflict, "frozen profile"):
            host.configure(self.connection, {"kind": "model", "value": "gpt"})
        with self.assertRaisesRegex(Conflict, "settled boundary"):
            self.host(pending=["t/step/1/model/ds"]).configure(self.connection, {"kind": "reasoning", "value": "high"})
        self.assertEqual(host.rows, [])

    def test_restart_restores_the_latest_choice_per_route(self):
        host = self.host()
        host.restore_reasoning({"configurations": [
            {"kind": "reasoning", "route": "ds", "value": "low"},
            {"kind": "reasoning", "route": "ds", "value": "max"},
            {"kind": "reasoning", "route": "ds-pro", "value": "xhigh"},  # no longer offered: configured default
            {"kind": "reasoning", "route": "removed", "value": "high"},
            {"operation": "rename", "name": "x"}]})
        self.assertEqual(host.owner.proposers["ds"].reasoning, "max")
        self.assertEqual(host.owner.child_proposers["ds"].reasoning, "max")
        self.assertIsNone(host.owner.proposers["ds-pro"].reasoning)


class ReasoningRoundTripTests(unittest.TestCase):
    """DeepSeek: "The `reasoning_content` in the thinking mode must be passed back to the API"
    (HTTP 400 on the first tool-call follow-up without it, seen live)."""

    def test_reply_reasoning_becomes_a_thinking_block(self):
        from kernelcat_executor.capabilities.messages import parse_turn
        turn = parse_turn("openai", {"choices": [{"finish_reason": "tool_calls", "message": {
            "reasoning_content": "read it first", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}]}}]}, {})
        self.assertEqual([block.type for block in turn.content], ["thinking", "toolCall"])
        self.assertEqual((turn.reasoning, turn.text), ("read it first", ""))

    def harness(self):
        return {**harness(), "messages": [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "old"}, {"type": "text", "text": "done"}]},
            {"role": "user", "content": "read a.py"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "need the file"},
                                              {"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "a.py"}}]},
            {"role": "toolResult", "toolCallId": "c1", "toolName": "read", "isError": False, "content": [{"type": "text", "text": "x = 42"}]}]}

    def test_reasoning_goes_back_within_the_current_turn_only(self):
        item = proposer(thinking="disabled")
        item.select_reasoning("low")
        messages = json.loads(item.prepare_harness(self.harness()).body_json)["messages"]
        assistants = [message for message in messages if message["role"] == "assistant"]
        self.assertNotIn("reasoning_content", assistants[0], "an earlier turn's reasoning only costs tokens")
        self.assertEqual(assistants[1]["reasoning_content"], "need the file")
        self.assertEqual(assistants[1]["tool_calls"][0]["function"]["name"], "read")

    def test_thinking_mode_turns_always_carry_the_field(self):
        # Seen live at effort low: a reply without reasoning, then HTTP 400 on the follow-up
        # until the assistant message carried reasoning_content, even empty.
        item = proposer(thinking="disabled")
        item.select_reasoning("low")
        value = self.harness()
        value["messages"][3]["content"] = value["messages"][3]["content"][1:]  # no thinking block
        messages = json.loads(item.prepare_harness(value).body_json)["messages"]
        assistants = [message for message in messages if message["role"] == "assistant"]
        self.assertEqual(assistants[1]["reasoning_content"], "")
        self.assertNotIn("reasoning_content", assistants[0])

    def test_child_requests_follow_the_same_rule(self):
        item = proposer(thinking="disabled")
        item.select_reasoning("high")
        turn = {"content": [{"type": "toolCall", "id": "c1", "name": "read_file", "arguments": {"path": "a"}}], "stop_reason": "tool_calls"}
        result = {"tool_call_id": "c1", "tool_name": "read_file", "status": "completed"}
        messages = json.loads(item.prepare({"task": "t", "turns": [{"step": 1, "assistant": turn, "results": [result]}]}, ()).body_json)["messages"]
        self.assertEqual([message.get("reasoning_content") for message in messages if message["role"] == "assistant"], [""])
        item.select_reasoning("off")
        messages = json.loads(item.prepare({"task": "t", "turns": [{"step": 1, "assistant": turn, "results": [result]}]}, ()).body_json)["messages"]
        self.assertFalse(any("reasoning_content" in message for message in messages))

    def test_projection_validation_accepts_assistant_thinking_only(self):
        from kernelcat_executor.contracts.harness import _messages
        _messages(self.harness()["messages"])
        with self.assertRaisesRegex(ValueError, "unsupported Pi content block"):
            _messages([{"role": "user", "content": [{"type": "thinking", "thinking": "x"}]}])
        with self.assertRaisesRegex(ValueError, "thinking block metadata"):
            _messages([{"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"}]}])

    def test_no_reasoning_is_sent_with_thinking_off(self):
        item = proposer(thinking="disabled")
        messages = json.loads(item.prepare_harness(self.harness()).body_json)["messages"]
        self.assertFalse(any("reasoning_content" in message for message in messages))

    def test_anthropic_encoding_drops_thinking_blocks(self):
        from kernelcat_executor.capabilities.messages import harness_conversation
        encoded = harness_conversation("anthropic", self.harness()["messages"])
        self.assertFalse(any(block.get("type") == "thinking" for message in encoded
                             if isinstance(message["content"], list) for block in message["content"]))

    def test_child_exchanges_carry_reasoning_in_the_current_turn(self):
        from kernelcat_executor.capabilities.messages import conversation
        turn = {"content": [{"type": "thinking", "thinking": "look"},
                            {"type": "toolCall", "id": "c1", "name": "read_file", "arguments": {"path": "a"}}],
                "stop_reason": "tool_calls"}
        result = {"tool_call_id": "c1", "tool_name": "read_file", "status": "completed"}
        messages = conversation("openai", {"task": "t", "turns": [{"step": 1, "assistant": turn, "results": [result]}]})
        self.assertEqual([message["role"] for message in messages], ["user", "assistant", "tool"])
        self.assertEqual(messages[1]["reasoning_content"], "look")

    def test_compaction_and_goal_review_ignore_reasoning(self):
        from kernelcat_executor.state.goal_progress import review_verdict
        verdict = review_verdict({"status": "completed", "data": {"content": [
            {"type": "thinking", "thinking": "weigh it"}, {"type": "text", "text": '{"status": "met", "summary": "ok"}'}]}})
        self.assertEqual(verdict["status"], "met")


if __name__ == "__main__":
    unittest.main()
