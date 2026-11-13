"""Action-bound assembled provider evidence, using local protocol fixtures only.

These tests do not reconstruct historical responses, invoke models or contact NPU
devices. Both transport and provider response bodies below are synthetic fixtures.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for source in ROOT.glob("source/packages/*/src"):
    sys.path.insert(0, str(source))

import httpx
from kernelcat_executor.capabilities.llm import APIProposer
from kernelcat_executor.capabilities.provider_evidence import (
    ProviderEvidence, capture_provider_evidence, failure_evidence,
)
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.capabilities.streaming import observe
from kernelcat_executor.contracts.models import Conflict, Policy, Reply, Task, ToolFailure, canonical
from kernelcat_executor.contracts.providers import WireOptions
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.requests import propose, recorded_provider_response, recorded_request
from kernelcat_executor.operations.retry_policy import retryable
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_executor.state.application import ApplicationJournal


SECRET = "fixture-private-api-key-never-retain"
USAGE = {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26}


def malformed(marker="private-tool-payload"):
    return {"id": "fixture-original-response", "choices": [{"finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path": "' + marker + '"'}}]}}],
        "usage": dict(USAGE), "provider_extension": {"preserve": [1, "中文", True]}}


class SnapshotView:
    """A changed reader projection, never a rewrite of the original Journal."""
    def __init__(self, store, mutate):
        self.path, self.store, self.mutate = store.path, store, mutate

    def snapshot(self):
        snapshot = deepcopy(self.store.snapshot())
        self.mutate(snapshot)
        return snapshot


class ProviderEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kb-provider-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = AgentJournal(self.root / "events.jsonl")
        self.store.initialize(50, Policy(context_bytes=262144))
        self.task = Task(id="task", scope="fixture", goal="Inspect frozen candidate")
        self.store.register_task(self.task.model_dump(mode="json"))
        self.app = ApplicationJournal(self.store, "application-fixture")
        self.app.command("create", {"task": self.task.model_dump(mode="json"), "workspace_mode": "isolated"})
        lease = self.app.command("acquire", {"worker_id": "fixture-worker", "mode": "headless"})["lease"]
        self.store.bind_application_lease("application-fixture", "fixture-worker", lease["lease_epoch"])
        self.source = {"source_sha256": "a" * 64, "evaluator_sha256": "b" * 64,
            "task_version": 2, "trial_id": "trial-fixture", "goal_id": "goal-fixture",
            "pi_session_id": "pi-fixture", "entry_id": "entry-fixture"}
        self.store.bind_application_context(self.source)
        self.gateway = Gateway(self.store, [], self.task.scope)
        self.transmitted = []

    def context(self):
        return {"policy": self.store.policy().model_dump(mode="json"),
            "task": self.task.model_dump(mode="json"), "history": []}

    def api(self, data=None, *, provider="openai", native=True, response=None, callback=None):
        def transport(request):
            self.transmitted.append(request.content)
            if callback:
                callback(request)
            return response if response is not None else httpx.Response(200, json=data)
        client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
        self.addCleanup(client.close)
        return APIProposer(provider, "https://fixture.invalid/v1", "fixture-model", SECRET,
            client=client, wire_options=WireOptions(native_turns=native, tool_choice="auto"))

    def call(self, api, identity="task/model/1"):
        return propose(self.gateway, api, identity, self.task.id, "primary", self.context(), (), ())

    def failure(self, outcome, *, category="protocol", usage=USAGE):
        self.assertEqual(outcome.status, "failed", outcome)
        details = outcome.data["failure_details"]
        self.assertEqual(details["category"], category)
        if usage is not None:
            self.assertEqual(details["usage"], usage)
        self.assertNotIn(SECRET, canonical(outcome))
        return details

    def test_parse_failure_retains_complete_assembled_data_and_original_bindings(self):
        data = malformed()
        result = self.call(self.api(data))
        details = self.failure(result)
        self.assertEqual(details["parse_error"]["exception_type"], "JSONDecodeError")
        self.assertEqual(details["parse_error"]["stage"], "parse_turn")
        self.assertNotIn("private-tool-payload", canonical(result))
        self.assertFalse(retryable(details))
        record = recorded_provider_response(self.store, "task/model/1")
        self.assertEqual(json.loads(record["data_json"]), data)
        self.assertFalse(record["redacted"])
        self.assertFalse(record["wire_complete"])
        self.assertEqual(record["capture_scope"], "assembled_provider_response")
        self.assertEqual(record["action_id"], "task/model/1")
        self.assertEqual(record["task_id"], self.task.id)
        self.assertEqual(record["source"], self.source)
        self.assertEqual(record["application_session_id"], "application-fixture")
        request = recorded_request(self.store, "task/model/1")
        self.assertEqual(record["request_digest"], request.fingerprint())
        self.assertEqual(self.transmitted, [request.body_json.encode()])
        self.assertNotIn(SECRET, request.body_json)
        snapshot = self.store.snapshot()
        self.assertEqual((snapshot["spent"], snapshot["reserved"]), (1, 0))
        path = Path(details["provider_response"]["locator"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(path.name, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_pydantic_error_omits_input_context_payload_and_credential(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                payload = "INPUT_VALUE_MUST_STAY_IN_PRIVATE_ARTIFACT " + SECRET
                if provider == "openai":
                    data = malformed()
                    data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps([payload])
                else:
                    data = {"stop_reason": "tool_use", "usage": USAGE, "content": [
                        {"type": "tool_use", "id": "one", "name": "read_file", "input": [payload]}]}
                identity = "task/model/" + provider
                result = self.call(self.api(data, provider=provider), identity)
                details = self.failure(result)
                diagnostic = details["parse_error"]
                self.assertEqual(diagnostic["exception_type"], "ValidationError")
                self.assertIn("dict_type", diagnostic["reason"])
                self.assertNotIn("INPUT_VALUE", canonical(result))
                self.assertNotIn("input_value", diagnostic["reason"])
                self.assertLessEqual(len(diagnostic["reason"]), 512)
                record = recorded_provider_response(self.store, identity)
                self.assertTrue(record["redacted"])
                self.assertNotIn(SECRET, canonical(record))
                self.assertIn("INPUT_VALUE_MUST_STAY_IN_PRIVATE_ARTIFACT", record["data_json"])

    def test_credential_echo_scrubs_response_before_retention_without_changing_failure(self):
        data = {"choices": [{"finish_reason": "stop", "message": {"content": "echo " + SECRET}}],
            "usage": USAGE, "metadata": {"authorization": "Bearer unrelated-token",
                "x_api_key": "another-private-token", "password": "not-our-configured-key"}}
        details = self.failure(self.call(self.api(data)), category="credential_echo")
        self.assertEqual(details["parse_error"]["exception_type"], "ToolFailure")
        record = recorded_provider_response(self.store, "task/model/1")
        self.assertTrue(record["redacted"])
        self.assertEqual(record["redactions"], ["configured_credential", "credential_field"])
        retained = canonical(record)
        for secret in (SECRET, "unrelated-token", "another-private-token", "not-our-configured-key"):
            self.assertNotIn(secret, retained)
        self.assertEqual(json.loads(record["data_json"])["choices"][0]["message"]["content"], "echo [REDACTED]")

    def test_json_escaped_credentials_are_redacted_before_the_only_artifact_write(self):
        variants = ("\\u0066" + SECRET[1:], "".join("\\u%04x" % ord(char) for char in SECRET))
        original_put = RuntimeArtifacts.put
        written = []
        def inspect_write(artifacts, raw, label):
            record = json.loads(raw)
            value = json.loads(record["data_json"])
            arguments = value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
            self.assertEqual(arguments, "[REDACTED]")
            self.assertTrue(record["redacted"])
            written.append(raw)
            return original_put(artifacts, raw, label)
        for index, encoded in enumerate(variants):
            with self.subTest(encoded=index):
                data = malformed()
                data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"token":"' + encoded + '"}'
                original = deepcopy(data)
                identity = "task/escaped/" + str(index)
                with patch.object(RuntimeArtifacts, "put", inspect_write):
                    result = self.call(self.api(data), identity)
                self.failure(result, category="credential_echo")
                record = recorded_provider_response(self.store, identity)
                self.assertIn("encoded_configured_credential", record["redactions"])
                self.assertEqual(data, original)
                self.assertNotIn(encoded, record["data_json"])
        self.assertEqual(len(written), 2)
        self.assertEqual(len(self.transmitted), 2)

    def test_nested_and_malformed_json_strings_cannot_hide_encoded_credentials(self):
        encoded = '{"token":"\\u0066' + SECRET[1:] + '"}'
        variants = ((json.dumps({"nested": encoded}), "completed"),
                    (json.dumps(json.dumps({"nested": encoded})), "failed"),
                    (encoded[:-1], "failed"))
        for index, (arguments, status) in enumerate(variants):
            with self.subTest(variant=index):
                data = malformed()
                data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
                identity = "task/nested/" + str(index)
                result = self.call(self.api(data), identity)
                self.assertEqual(result.status, status)
                if status == "failed":
                    self.failure(result)
                directory = self.root / "model-responses" / hashlib.sha256(identity.encode()).hexdigest()
                files = list(directory.iterdir())
                self.assertEqual(len(files), 1)
                record = json.loads(files[0].read_bytes())
                self.assertTrue(record["redacted"])
                self.assertEqual(json.loads(record["data_json"])["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], "[REDACTED]")

    def test_nested_sensitive_fields_are_redacted_and_clean_arguments_are_unchanged(self):
        sensitive = '{"metadata":"{\\"authorization\\":\\"other-private-token\\"}"}'
        clean = ' { "nested": "{\\"path\\":\\"candidate.py\\"}", "text": "\\u4e2d\\u6587" } '
        for index, arguments in enumerate((sensitive, clean)):
            data = malformed()
            data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
            identity = "task/fields/" + str(index)
            self.assertEqual(self.call(self.api(data), identity).status, "completed")
            directory = self.root / "model-responses" / hashlib.sha256(identity.encode()).hexdigest()
            record = json.loads(next(directory.iterdir()).read_bytes())
            retained = json.loads(record["data_json"])["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
            if index == 0:
                self.assertTrue(record["redacted"])
                self.assertNotIn("other-private-token", record["data_json"])
                self.assertEqual(json.loads(json.loads(retained)["metadata"])["authorization"], "[REDACTED]")
            else:
                self.assertFalse(record["redacted"])
                self.assertEqual(retained, arguments)

    def test_decoding_limits_drop_evidence_without_affecting_original_provider_failure(self):
        nested = "".join("\\u%04x" % ord(char) for char in SECRET)
        for _ in range(10):
            nested = json.dumps(nested)
        data = malformed()
        data["provider_extension"]["nested"] = nested
        details = self.failure(self.call(self.api(data), "task/deep"))
        self.assertEqual(details["provider_response"], {"status": "unavailable", "reason": "redaction_limit", "wire_complete": False})
        with patch("kernelcat_executor.capabilities.provider_evidence._DECODE_CHARACTERS", 16):
            details = self.failure(self.call(self.api(malformed()), "task/work-limit"))
        self.assertEqual(details["provider_response"]["reason"], "redaction_limit")
        self.assertEqual(details["parse_error"]["exception_type"], "JSONDecodeError")
        self.assertIn("safe redaction exceeded", details["parse_error"]["reason"])
        self.assertFalse((self.root / "model-responses").exists())
        self.assertEqual(self.store.snapshot()["spent"], 2)
        self.assertEqual(self.store.snapshot()["reserved"], 0)

    def test_encoded_credential_in_exception_reason_is_not_returned_in_receipt(self):
        encoded = "\\u0066" + SECRET[1:]
        with patch("kernelcat_executor.capabilities.llm.parse_turn", side_effect=ValueError("invalid " + encoded)):
            result = self.call(self.api(malformed()))
        details = self.failure(result)
        self.assertEqual(details["parse_error"]["exception_type"], "ValueError")
        self.assertEqual(details["parse_error"]["reason"], "[REDACTED]")
        self.assertNotIn(encoded, canonical(result))

    def test_deep_json_diagnostic_cannot_replace_original_parse_failure(self):
        depth = sys.getrecursionlimit() + 200
        nested = "[" * depth + "0" + "]" * depth
        with patch("kernelcat_executor.capabilities.llm.parse_turn", side_effect=ValueError(nested)):
            result = self.call(self.api(malformed()))
        details = self.failure(result)
        self.assertEqual(details["parse_error"]["exception_type"], "ValueError")
        self.assertIn("Diagnostic omitted: safe redaction", details["parse_error"]["reason"])
        self.assertEqual(self.store.snapshot()["spent"], 1)
        self.assertEqual(self.store.snapshot()["reserved"], 0)

    def test_existing_failure_categories_usage_and_retry_decisions_are_unchanged(self):
        incomplete = malformed()
        incomplete["choices"][0]["finish_reason"] = "length"
        details = self.failure(self.call(self.api(incomplete), "task/truncated"), category="incomplete_output")
        self.assertFalse(retryable(details))
        self.assertEqual(details["provider_response"]["status"], "recorded")
        rate = httpx.Response(429, json={"error": SECRET}, headers={"retry-after": "4"})
        details = self.failure(self.call(self.api(response=rate), "task/rate-limit"), category="rate_limit", usage=None)
        self.assertTrue(retryable(details))
        self.assertEqual(details["http_status"], 429)
        self.assertEqual(details["retry_after"], 4)
        self.assertNotIn("parse_error", details)
        self.assertEqual(details["provider_response"]["reason"], "no_assembled_response")
        self.assertEqual(len(self.transmitted), 2)  # no new retry/fallback hidden in capture

    def test_artifact_write_failure_preserves_protocol_outcome_and_settlement(self):
        with patch.object(RuntimeArtifacts, "put", side_effect=OSError("private storage detail " + SECRET)):
            result = self.call(self.api(malformed()))
        details = self.failure(result)
        self.assertEqual(details["provider_response"], {"status": "unavailable",
            "reason": "artifact_capture_failed", "error_type": "OSError", "wire_complete": False})
        self.assertEqual(details["parse_error"]["exception_type"], "JSONDecodeError")
        self.assertEqual(self.store.snapshot()["reserved"], 0)
        self.assertEqual(self.store.snapshot()["spent"], 1)
        self.assertEqual(failure_evidence(), {})

    def test_artifact_path_setup_failure_does_not_prevent_original_provider_call(self):
        with patch("kernelcat_executor.capabilities.provider_evidence.RuntimeArtifacts",
                   side_effect=OSError("cannot resolve evidence directory " + SECRET)):
            result = self.call(self.api(malformed()))
        details = self.failure(result)
        self.assertEqual(details["provider_response"]["reason"], "artifact_capture_failed")
        self.assertEqual(details["provider_response"]["error_type"], "OSError")
        self.assertEqual(len(self.transmitted), 1)
        self.assertEqual(self.store.snapshot()["reserved"], 0)
        self.assertEqual(self.store.snapshot()["spent"], 1)

    def test_concurrent_operations_keep_call_and_source_identity_and_reset_context(self):
        barrier = Barrier(2)
        def run(index):
            self.store.bind_application_context({**self.source, "child_id": "child-" + str(index),
                "source_sha256": str(index) * 64}, local=True)
            identity = "task/child/" + str(index) + "/model"
            api = self.api(malformed("payload-" + str(index)), callback=lambda _: barrier.wait(timeout=5))
            result = self.call(api, identity)
            return identity, result, failure_evidence()
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = list(pool.map(run, (1, 2)))
        locators = []
        for index, (identity, result, after) in enumerate(calls, 1):
            details = self.failure(result)
            record = recorded_provider_response(self.store, identity)
            self.assertEqual(after, {})
            self.assertEqual(record["source"]["child_id"], "child-" + str(index))
            self.assertEqual(record["source"]["source_sha256"], str(index) * 64)
            self.assertIn("payload-" + str(index), record["data_json"])
            self.assertNotIn("payload-" + str(3 - index), record["data_json"])
            locators.append(details["provider_response"]["locator"])
        self.assertEqual(len(set(locators)), 2)
        self.assertEqual(failure_evidence(), {})
        self.assertEqual(self.store.snapshot()["spent"], 2)
        self.assertEqual(self.store.snapshot()["reserved"], 0)

    def test_unexpected_parser_exception_keeps_unknown_fence_and_resets_context(self):
        with patch("kernelcat_executor.capabilities.llm.parse_turn", side_effect=RuntimeError("unknown-parser-fault")):
            result = self.call(self.api(malformed()))
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.data, {"error_type": "RuntimeError"})
        self.assertFalse(self.store.snapshot()["actions"]["task/model/1"]["terminal"])
        self.assertEqual(failure_evidence(), {})
        request = recorded_request(self.store, "task/model/1")
        evidence = ProviderEvidence(self.store, self.store.snapshot()["actions"]["task/model/1"], request)
        with capture_provider_evidence(evidence):
            before = failure_evidence()
            with self.assertRaisesRegex(RuntimeError, "nested-error"):
                with capture_provider_evidence(None):
                    self.assertEqual(failure_evidence(), {})
                    raise RuntimeError("nested-error")
            self.assertEqual(failure_evidence(), before)
        self.assertEqual(failure_evidence(), {})

    def test_reader_rejects_changed_artifact_cross_action_and_source_binding(self):
        first = self.call(self.api(malformed()), "task/first")
        second = self.call(self.api(malformed("second")), "task/second")
        ref = first.data["failure_details"]["provider_response"]
        second_ref = second.data["failure_details"]["provider_response"]
        def swapped(snapshot):
            snapshot["actions"]["task/first"]["outcome"]["data"]["failure_details"]["provider_response"] = second_ref
        with self.assertRaises(Conflict):
            recorded_provider_response(SnapshotView(self.store, swapped), "task/first")
        def changed_source(snapshot):
            snapshot["actions"]["task/first"]["application_context"]["source_sha256"] = "c" * 64
        with self.assertRaisesRegex(Conflict, "original admission"):
            recorded_provider_response(SnapshotView(self.store, changed_source), "task/first")
        path = Path(ref["locator"])
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b"private-tool-payload", b"private-tool-payloae"))
        with self.assertRaisesRegex(Conflict, "retained digest"):
            recorded_provider_response(self.store, "task/first")
        self.assertEqual(len(self.transmitted), 2)

    def test_reader_rejects_original_request_tamper_and_does_not_invent_legacy_evidence(self):
        self.call(self.api(malformed()))
        def changed_request(snapshot):
            action = snapshot["actions"]["task/model/1"]["action"]
            body = json.loads(action["arguments"]["request"]["body_json"])
            body["model"] = "tampered-model"
            action["arguments"]["request"]["body_json"] = canonical(body)
        with self.assertRaisesRegex(Conflict, "request digest mismatch"):
            recorded_provider_response(SnapshotView(self.store, changed_request), "task/model/1")
        def lost_evidence(snapshot):
            snapshot["actions"]["task/model/1"]["outcome"]["data"]["failure_details"].pop("provider_response")
        with self.assertRaisesRegex(Conflict, "no retained provider response"):
            recorded_provider_response(SnapshotView(self.store, lost_evidence), "task/model/1")
        self.assertEqual(len(self.transmitted), 1)

    def test_success_no_sink_and_legacy_proposer_remain_compatible(self):
        success = {"choices": [{"finish_reason": "stop", "message": {"content": "original answer"}}], "usage": USAGE}
        result = self.call(self.api(success))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["usage"], USAGE)
        self.assertEqual(result.data["content"], [{"type": "text", "text": "original answer"}])
        self.assertNotIn("failure_details", result.data)
        artifacts = list((self.root / "model-responses").glob("*/*"))
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(json.loads(json.loads(artifacts[0].read_text())["data_json"]), success)
        direct = self.api(malformed())
        with self.assertRaises(ToolFailure) as caught:
            direct.invoke(direct.prepare(self.context(), ()))
        self.assertEqual(caught.exception.details["category"], "protocol")
        self.assertEqual(caught.exception.details["usage"], USAGE)
        self.assertNotIn("provider_response", caught.exception.details)
        class Legacy:
            def propose(self, context, tools):
                return Reply(tool="finish", arguments={"reason": "fixture"}, usage={"total_tokens": 2})
        legacy = self.call(Legacy(), "task/legacy")
        self.assertEqual(legacy.status, "completed")
        self.assertEqual(legacy.data["tool"], "finish")
        self.assertEqual(len(list((self.root / "model-responses").glob("*/*"))), 1)
        self.assertEqual(failure_evidence(), {})

    def test_sse_failure_retains_only_actual_assembled_response_with_explicit_wire_boundary(self):
        values = [{"choices": [{"index": 0, "delta": {"content": "before tool", "tool_calls": [
            {"index": 0, "id": "call-stream", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path": "malformed-stream"'}}]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": USAGE}]
        sse = "".join("data: " + json.dumps(item) + "\n\n" for item in values) + "data: [DONE]\n\n"
        deltas = []
        with observe(deltas.append):
            result = self.call(self.api(response=httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})))
        details = self.failure(result)
        self.assertEqual(details["parse_error"]["exception_type"], "JSONDecodeError")
        self.assertTrue(json.loads(self.transmitted[0])["stream"])
        record = recorded_provider_response(self.store, "task/model/1")
        assembled = json.loads(record["data_json"])
        self.assertEqual(assembled["usage"], USAGE)
        self.assertEqual(assembled["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(assembled["choices"][0]["message"]["content"], "before tool")
        self.assertFalse(record["wire_complete"])
        self.assertNotIn("sse", record)
        self.assertEqual(deltas, [{"type": "text_delta", "delta": "before tool"}])

    def test_non_native_tool_reply_failure_retains_exact_parse_stage(self):
        details = self.failure(self.call(self.api(malformed(), native=False)))
        self.assertEqual(details["parse_error"]["stage"], "parse_tool_reply")
        self.assertEqual(details["parse_error"]["exception_type"], "JSONDecodeError")
        self.assertEqual(json.loads(recorded_provider_response(self.store, "task/model/1")["data_json"]), malformed())


if __name__ == "__main__":
    unittest.main()
