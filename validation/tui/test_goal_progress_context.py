"""Journal-bound model progress and wire commitments; no network or device effects."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import hashlib, json, subprocess, sys, unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [
    *ROOT.glob("source/packages/*/src"),
    ROOT / "source/apps/worker/src",
    ROOT / "validation",
]:
    sys.path.insert(0, str(path))
import httpx
import test_subagent_recovery as support
from kernelcat_executor.capabilities.llm import APIProposer
from kernelcat_executor.contracts.context import (
    validate_submission,
    validate_context_source,
)
from kernelcat_executor.contracts.harness import validate_harness
from kernelcat_executor.contracts.models import (
    Action,
    BudgetExceeded,
    Conflict,
    Outcome,
    Policy,
    canonical,
    digest,
)
from kernelcat_executor.contracts.providers import WireOptions
from kernelcat_executor.contracts.requests import ModelRequest
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_executor.operations.requests import propose
from kernelcat_executor.state.application import ApplicationJournal
from kernelcat_executor.state.goal_progress import (
    goal_usage,
    model_call_progress,
    with_model_call_progress,
)
from kernelcat_worker.application_host import ApplicationHost
from kernelcat_worker.application_goal_review import review


class GoalContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.RecoveryTests(methodName="runTest")
        self.fixture.setUp()
        self.fixture.store = AgentJournal(self.fixture.base / "goal-state/events.jsonl")
        self.fixture.store.initialize(
            300,
            Policy(routes=("primary", "secondary"), max_steps=25, context_bytes=262144),
        )
        self.owner, self.rpc = self.fixture.authority()
        self.store = self.fixture.store
        self.app = ApplicationJournal(self.store, "goal-context")
        self.app.command(
            "create",
            {
                "task": self.fixture.task.model_dump(mode="json"),
                "workspace_mode": "isolated",
                "goal": {
                    "goal_id": "goal-one",
                    "status": "active",
                    "objective": "Declared task",
                    "task_version": 1,
                    "on_detach": "pause",
                    "max_model_turns": 20,
                },
            },
        )
        self.lease = self.app.command(
            "acquire", {"worker_id": "worker", "mode": "headless"}
        )["lease"]
        self.store.bind_application_lease(
            "goal-context", "worker", self.lease["lease_epoch"]
        )
        self.requests = []
        self.fail_next = False

        def transport(request):
            self.requests.append(request.content.decode())
            payload = json.loads(request.content)
            if self.fail_next:
                self.fail_next = False
                return httpx.Response(503, json={"error": "fixture known failure"})
            text = (
                json.dumps(
                    {
                        "status": "continue",
                        "summary": "Continue from original evidence.",
                    }
                )
                if "independently assess" in payload["messages"][0]["content"]
                else "Original fixture answer."
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 10},
                },
            )

        self.client = httpx.Client(transport=httpx.MockTransport(transport))
        self.api = APIProposer(
            "openai",
            "http://127.0.0.1/v1",
            "fixture",
            "fixture-key",
            client=self.client,
            wire_options=WireOptions(native_turns=True, tool_choice="auto"),
        )
        self.owner.proposers = {"primary": self.api, "secondary": self.api}
        self.owner.child_proposers = dict(self.owner.proposers)
        self.rpc.active = False
        self.rpc.hello()

    def tearDown(self):
        self.client.close()
        self.fixture.tearDown()

    def harness(self, context):
        return {
            "schema_version": "kernelcat.pi-harness/v1",
            "adapter": "pi@1.5.0",
            "system_prompt": "KERNELCAT_CONTEXT_V1\n"
            + canonical(context)
            + "\nEND_KERNELCAT_CONTEXT_V1\nFixture instructions.",
            "messages": [{"role": "user", "content": "Follow the declared task."}],
            "tools": [],
        }

    def model(self, context, route="primary"):
        harness = self.harness(context)
        return self.rpc.model(
            {"route": route, "context": context, "harness": harness}
        ), harness

    def checkpoint(self, name, status="completed"):
        action = Action(id=name, task=self.fixture.task.id, tool="__propose__")
        self.owner.gateway.internal(action, 1, lambda: Outcome(status=status))
        return action

    def host(self):
        host = ApplicationHost.__new__(ApplicationHost)
        host.owner = self.owner
        host.task = self.fixture.task
        host.journal = self.app
        host.config = SimpleNamespace(goal_review=SimpleNamespace(context_bytes=131072))
        host.status = lambda: self.app.state()
        return host

    def assert_fact(self, fact, used):
        self.assertEqual(fact["max_model_turns"], 20)
        self.assertEqual(fact["model_calls_admitted"], used)
        self.assertEqual(fact["remaining_calls_including_this_request"], 20 - used)
        self.assertEqual(fact["current_request_cost"], 1)
        self.assertTrue(fact["concurrent_calls_may_consume_remaining"])
        rows = self.store.events()
        prefix = rows[: fact["observed_seq"] + 1]
        from kernelcat_executor.state.application import project

        goal = project(prefix, "goal-context")["goal"]
        self.assertEqual(goal["goal_id"], fact["goal_id"])
        self.assertEqual(
            goal_usage(list(self.store._events(prefix)), goal)["model_turns"], used
        )

    def test_parent_child_compact_review_request_bytes_share_one_counter(self):
        context = self.rpc.prepare_turn()["context_source"]
        self.assert_fact(context["goal_progress"], 0)
        parent, harness = self.model(context)
        self.assertEqual(parent["text"], "Original fixture answer.")
        action, _ = self.fixture.begin(self.rpc, identity="real-child")
        child = self.rpc.background[action.id].effects
        result = child.model("primary")
        self.rpc.plugin_settle(
            {"permit": action.id, "status": "completed", "text": result["text"]}
        )
        compact = {
            "schema_version": "kernelcat.pi-harness/v1",
            "adapter": "pi@1.5.0",
            "system_prompt": "Summarize original facts.",
            "messages": [{"role": "user", "content": "Original conversation."}],
            "tools": [],
        }
        host = self.host()
        host.compact({"harness": compact, "input_id": "compact"})
        review(
            host,
            SimpleNamespace(worker=self.lease),
            self.app.state(),
            {"input_id": "review-input"},
        )
        rows = [
            row
            for row in self.store.snapshot()["actions"].values()
            if row["action"]["tool"] == "__propose__"
        ]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(self.requests), 4)
        for index, (row, wire) in enumerate(zip(rows, self.requests, strict=True)):
            args = row["action"]["arguments"]
            request = ModelRequest.model_validate_json(canonical(args["request"]))
            self.assertEqual(request.body_json, wire)
            self.assertEqual(request.fingerprint(), args["request_digest"])
            payload = json.loads(wire)
            if index == 0:
                encoded = (
                    payload["messages"][0]["content"]
                    .split("KERNELCAT_CONTEXT_V1\n", 1)[1]
                    .split("\nEND_KERNELCAT_CONTEXT_V1", 1)[0]
                )
                self.assertEqual(args["harness_digest"], digest(harness))
            elif index == 1:
                encoded = payload["messages"][1]["content"]
            elif index == 2:
                encoded = payload["messages"][0]["content"].split(
                    "Retain these authoritative facts verbatim in the summary:\n", 1
                )[1]
            else:
                encoded = payload["messages"][1]["content"]
            fact = json.loads(encoded)["goal_progress"]
            self.assert_fact(fact, index)
            # The protected observation is prepared before this request's admission.
            # Its named remainder includes this request; the actual Journal now counts it.
            admission = next(
                event
                for event in self.store._events(self.store.events())
                if event["kind"] == "admitted"
                and event["action"]["id"] == row["action"]["id"]
            )
            prefix = [
                event
                for event in self.store._events(self.store.events())
                if event["seq"] <= admission["seq"]
            ]
            self.assertEqual(
                goal_usage(prefix, self.app.state()["goal"])["model_turns"], index + 1
            )
            self.assertEqual(
                fact["remaining_calls_including_this_request"]
                - fact["current_request_cost"],
                19 - index,
            )
        self.assertEqual(model_call_progress(self.store)["model_calls_admitted"], 4)

    def test_known_provider_fallback_refreshes_the_snapshot_before_new_digest(self):
        context = self.rpc.prepare_turn()["context_source"]
        self.fail_next = True
        failed, _ = self.model(context)
        self.assertTrue(failed["failed"])
        refreshed = failed["context_source"]
        self.assert_fact(refreshed["goal_progress"], 1)
        result, _ = self.model(refreshed, "secondary")
        self.assertEqual(result["text"], "Original fixture answer.")
        self.assertEqual(model_call_progress(self.store)["model_calls_admitted"], 2)

    def test_client_and_legacy_transform_cannot_forge_progress(self):
        context = self.rpc.prepare_turn()["context_source"]
        tampered = json.loads(canonical(context))
        tampered["goal_progress"]["remaining_calls_including_this_request"] = 200
        with self.assertRaises(Conflict):
            validate_submission(context, tampered, self.store.policy())
        with self.assertRaises(Conflict):
            validate_harness(
                self.harness(tampered),
                context,
                self.rpc.specs,
                self.store.policy().context_bytes,
            )
        self.owner.transform_context = lambda ctx: {
            **ctx,
            "goal_progress": {**ctx["goal_progress"], "max_model_turns": 999},
        }
        with self.assertRaisesRegex(Conflict, "protected goal_progress"):
            self.owner._context(self.fixture.task, self.store.snapshot(), None)
        self.assertFalse(self.requests)

    def test_unbounded_absent_and_cleared_goals_do_not_invent_a_limit(self):
        store = AgentJournal(self.fixture.base / "unlimited/events.jsonl")
        store.initialize(None, Policy())
        task = self.fixture.task.model_dump(mode="json")
        store.register_task(task)
        app = ApplicationJournal(store, "unlimited")
        app.command(
            "create",
            {
                "task": task,
                "goal": {
                    "goal_id": "unlimited",
                    "status": "active",
                    "objective": "Continue",
                    "on_detach": "pause",
                },
            },
        )
        lease = app.command("acquire", {"worker_id": "worker", "mode": "headless"})[
            "lease"
        ]
        store.bind_application_lease("unlimited", "worker", lease["lease_epoch"])
        self.assertIsNone(model_call_progress(store))
        self.assertNotIn(
            "goal_progress", with_model_call_progress({"policy": {}}, store)
        )
        self.assertIsNone(model_call_progress(SimpleNamespace()))
        self.app.command(
            "accept",
            {
                "input_id": "clear",
                "kind": "control",
                "text": "clear",
                "initiator": "user",
                "channel": "sdk",
            },
        )
        self.app.command("control", {"input_id": "clear", "operation": "clear"})
        self.assertIsNone(model_call_progress(self.store))

    def test_unknown_counts_as_admitted_and_projection_does_not_write_events(self):
        self.checkpoint("original-unknown", "unknown")
        before = self.store.path.read_bytes()
        self.assert_fact(model_call_progress(self.store), 1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot()["reserved"], 1)

    def test_new_goal_restarts_only_its_original_start_seq(self):
        self.checkpoint("old-call")
        self.app.command(
            "accept",
            {
                "input_id": "new-goal",
                "kind": "control",
                "text": "set",
                "initiator": "user",
                "channel": "sdk",
            },
        )
        self.app.command(
            "control",
            {"input_id": "new-goal", "operation": "set", "objective": "New user goal"},
        )
        fact = model_call_progress(self.store)
        self.assertEqual(fact["goal_id"], "new-goal")
        self.assert_fact(fact, 0)

    def test_eight_competing_requests_with_three_remaining_admit_only_three(self):
        for index in range(17):
            self.checkpoint(f"prior-{index}")
        barrier = Barrier(8)

        def request(index):
            context = with_model_call_progress(
                {"policy": self.store.policy().model_dump(mode="json")}, self.store
            )
            self.assert_fact(context["goal_progress"], 17)
            barrier.wait(20)
            try:
                return propose(
                    self.owner.gateway,
                    self.api,
                    f"race-{index}",
                    self.fixture.task.id,
                    "primary",
                    context,
                    (),
                    (),
                ).status
            except BudgetExceeded:
                return "denied"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(request, range(8)))
        self.assertEqual(results.count("completed"), 3)
        self.assertEqual(results.count("denied"), 5)
        self.assert_fact(model_call_progress(self.store), 20)
        self.assertEqual(len(self.requests), 3)

    def test_node_compiler_preserves_facts_and_actual_input_hashes(self):
        context = self.rpc.prepare_turn()["context_source"]
        path = self.fixture.base / "source.json"
        path.write_text(canonical(context))
        module = (ROOT / "source/apps/agent/src/context/compiler.ts").as_uri()
        command = """import {readFile} from 'node:fs/promises';import {createHash} from 'node:crypto';import {compileContext} from MODULE;const source=JSON.parse(await readFile(process.argv[1]));const selected=compileContext(source,{harnessConversation:true});const input=JSON.stringify({schema_version:'kernelcat.pi-harness/v1',adapter:'pi@1.5.0',system_prompt:'KERNELCAT_CONTEXT_V1\\n'+selected.canonical+'\\nEND_KERNELCAT_CONTEXT_V1\\nFixture.',messages:[{role:'user',content:'Fixture.'}],tools:[]});process.stdout.write(JSON.stringify({fact:selected.context.goal_progress,input,sha256:createHash('sha256').update(input).digest('hex')}));""".replace(
            "MODULE", json.dumps(module)
        )
        raw = subprocess.run(
            ["node", "--input-type=module", "-e", command, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(raw.stdout)
        self.assertEqual(value["fact"], context["goal_progress"])
        self.assertEqual(
            value["sha256"], hashlib.sha256(value["input"].encode()).hexdigest()
        )
        # This is the exact harness serialized by managedContextCommit; mutation would change its input digest.
        original = json.loads(value["input"])
        validate_harness(
            original, context, self.rpc.specs, self.store.policy().context_bytes
        )
        original["system_prompt"] = original["system_prompt"].replace(
            '"max_model_turns":20', '"max_model_turns":200'
        )
        self.assertNotEqual(
            value["sha256"], hashlib.sha256(json.dumps(original).encode()).hexdigest()
        )

    def test_context_pruning_preserves_progress_and_rejects_mandatory_overflow(self):
        from kernelcat_executor.operations.context import project, ContextOverflow
        from kernelcat_executor.contracts.models import Task

        policy = Policy(context_bytes=4096, observation_bytes=4096)
        for index in range(5):
            action = Action(
                id=f"large-tool-{index}",
                task=self.fixture.task.id,
                tool="__large_fixture__",
            )
            self.owner.gateway.internal(
                action, 0, lambda: Outcome(data={"text": "x" * 1500})
            )
        facts = model_call_progress(self.store)
        selected = project(
            self.fixture.task, policy, self.store.snapshot(), [], goal_progress=facts
        )
        self.assertEqual(selected["goal_progress"], facts)
        self.assertLessEqual(len(canonical(selected).encode()), 4096)
        oversized = Task(id="too-large", scope="fixture", goal="x" * 5000)
        with self.assertRaises(ContextOverflow):
            project(oversized, policy, self.store.snapshot(), [], goal_progress=facts)
        self.assertFalse(self.requests)

    def test_actual_managed_commit_and_owner_validator_bind_the_protected_facts(self):
        from kernelcat_executor.contracts.runtime import (
            ContextAdmission,
            bytes_digest,
            validate_runtime,
        )
        from kernelcat_executor.state.runtime_context import context_event
        from kernelcat_executor.state.runtime_state import project as runtime_project

        context = self.rpc.prepare_turn()["context_source"]
        unknown = {"state": "unknown", "reason": "Offline managed context fixture."}
        policy = self.store.policy().model_dump(mode="json")
        policy_digest = "sha256:" + digest(policy)
        version = "sha256:" + "a" * 64
        external = {
            "kb_version": unknown,
            "harness_version": unknown,
            "memory_revision": {"memory_id": "memory", "revision": 1},
            "memory_epoch": 0,
            "evidence_cutoff": 0,
        }
        trial = {
            "kind": "trial",
            "trial_id": "trial",
            "revision": 1,
            "parent_program_id": "program",
            "working_program_id": "program",
            "proposal_revision": {"proposal_id": "proposal", "revision": 1},
            "external_snapshot": external,
            "policy_digest": policy_digest,
            "trial_feedback_cursor": 0,
            "execution_state": "GENERATING",
        }
        record = {
            "run": {
                "run_id": "run",
                "policy_digest": policy_digest,
                "version_lock_digest": version,
            },
            "trial": trial,
            "branch": {
                "kind": "branch",
                "branch_id": "branch",
                "revision": 1,
                "context_revision": 0,
            },
            "session": {
                "kind": "agent_session",
                "agent_session_id": "session",
                "revision": 1,
                "state": "RUNNING",
            },
            "parent_program": {"program_id": "program"},
            "compiler_version": unknown,
            "references": [],
            "required_ref_ids": [],
            "graph": {
                "objects": [],
                "version_locks": [
                    {
                        "version_lock_digest": version,
                        "lock": {"rules": {"prune_policy": unknown}},
                    }
                ],
            },
            "version_lock": {"lock": {"rules": {"prune_policy": unknown}}},
            "manifest_digest": "sha256:" + "b" * 64,
            "run_budget": {"limit": 300, "spent": 0, "reserved": 0},
            "trial_budget": {"limit": 300, "spent": 0, "reserved": 0},
            "generation": 1,
            "lease": {"epoch": 1},
            "control_revision": 1,
            "status": "active",
            "policy": policy,
            "tool_names": ["read_file"],
        }
        state = {
            "account": {"account_id": "account"},
            "runtime_runs": {"run": record},
            "runtime_trials": {"run": {"trial": record}},
            "actions": {},
            "runtime_knowledge_trials": {},
            "limit": 300,
            "spent": 0,
            "reserved": 0,
        }
        runtime = runtime_project(state, "run", "trial")
        context["runtime"] = runtime["stamp"]
        harness = self.harness(context)
        input_path = self.fixture.base / "managed.json"
        input_path.write_text(
            canonical({"state": runtime, "source": context, "harness": harness})
        )
        compiler = (ROOT / "source/apps/agent/src/context/compiler.ts").as_uri()
        managed = (ROOT / "source/apps/agent/src/context/managed.ts").as_uri()
        command = r"""import {readFile} from 'node:fs/promises';import {compileContext} from COMPILER;import {managedContextCommit} from MANAGED;
const value=JSON.parse(await readFile(process.argv[1]));const selected=compileContext(value.source,{harnessConversation:true});
const harness={...value.harness,system_prompt:'KERNELCAT_CONTEXT_V1\n'+selected.canonical+'\nEND_KERNELCAT_CONTEXT_V1\nFixture instructions.'};
const commit=managedContextCommit(value.state,selected.context,harness,['read_file']);process.stdout.write(JSON.stringify({context:selected.context,harness,commit}));
""".replace("COMPILER", json.dumps(compiler)).replace("MANAGED", json.dumps(managed))
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", command, str(input_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        commit = validate_runtime(value["commit"], "ContextCommit")
        validate_harness(
            value["harness"],
            value["context"],
            self.rpc.specs,
            self.store.policy().context_bytes,
        )
        self.assertEqual(value["context"]["goal_progress"], context["goal_progress"])
        before = self.store.path.read_bytes()
        event = context_event(
            state,
            record,
            ContextAdmission(commit, context, value["context"], value["harness"]),
        )
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(event["input_json"], commit["input_json"])
        checksum = bytes_digest(commit["input_json"].encode())
        self.assertEqual(checksum, commit["manifest"]["input_digest"])
        self.assertEqual(checksum, commit["turn"]["input_digest"])
        self.assertEqual(checksum, commit["capsule"]["input_digest"])
        self.assertEqual(checksum, event["references"][0]["digest"])
        self.assertIn(
            '"goal_progress":', json.loads(commit["input_json"])["system_prompt"]
        )
        self.assertFalse(self.requests)
        appended = json.loads(canonical(value["harness"]))
        appended["system_prompt"] += " Post-commit mutation."
        with self.assertRaisesRegex(Conflict, "actual selected input bytes"):
            context_event(
                state,
                record,
                ContextAdmission(commit, context, value["context"], appended),
            )
        changed = json.loads(canonical(commit))
        changed["input_json"] = json.dumps(appended)
        with self.assertRaisesRegex(Conflict, "context input digest"):
            context_event(
                state,
                record,
                ContextAdmission(changed, context, value["context"], appended),
            )
        forged = json.loads(canonical(value["context"]))
        forged["goal_progress"]["remaining_calls_including_this_request"] = 21
        with self.assertRaisesRegex(Conflict, "protected goal_progress"):
            context_event(
                state,
                record,
                ContextAdmission(commit, context, forged, value["harness"]),
            )
        print(
            "MANAGED_CONTEXT_EVIDENCE "
            + json.dumps(
                {
                    "input_digest": checksum,
                    "input_bytes": commit["manifest"]["input_bytes"],
                    "original_journal_seq": context["goal_progress"]["observed_seq"],
                    "real_node_managed_commit": True,
                    "real_python_context_event": True,
                    "post_commit_mutation_rejected": True,
                    "manifest_digest_mutation_rejected": True,
                    "forged_remaining_rejected": True,
                    "external_requests": 0,
                },
                sort_keys=True,
            )
        )

    def test_pending_admission_is_counted_and_forged_metadata_is_replaced(self):
        action = Action(
            id="pending-parent", task=self.fixture.task.id, tool="__propose__"
        )
        self.store.admit(action, 1, self.store.policy())
        before = self.store.path.read_bytes()
        value = with_model_call_progress(
            {
                "goal_progress": {
                    "remaining_calls_including_this_request": 999,
                    "secret": "do-not-copy",
                }
            },
            self.store,
        )
        self.assert_fact(value["goal_progress"], 1)
        self.assertNotIn("secret", value["goal_progress"])
        self.assertNotIn("do-not-copy", canonical(value))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot()["reserved"], 1)
        self.assertFalse(self.requests)

    def test_legacy_provider_fallback_refreshes_progress_for_each_request(self):
        self.owner.external_context = False
        self.rpc.prepare_turn()
        self.fail_next = True
        first = self.rpc.model({"route": "primary"})
        self.assertTrue(first["failed"])
        final = self.rpc.model({"route": "secondary"})
        self.assertEqual(final["text"], "Original fixture answer.")
        self.assertEqual(len(self.requests), 2)
        for index, wire in enumerate(self.requests):
            context = json.loads(json.loads(wire)["messages"][1]["content"])
            self.assert_fact(context["goal_progress"], index)
        self.assertEqual(model_call_progress(self.store)["model_calls_admitted"], 2)

    def test_nested_failure_usage_counts_toward_the_existing_token_limit(self):
        self.app.command(
            "accept",
            {
                "input_id": "token-limit",
                "kind": "control",
                "text": "set",
                "initiator": "user",
                "channel": "sdk",
            },
        )
        self.app.command(
            "control",
            {
                "input_id": "token-limit",
                "operation": "set",
                "objective": "Bounded work",
                "token_budget": 15,
            },
        )
        action = Action(
            id="failed-with-usage", task=self.fixture.task.id, tool="__propose__"
        )
        self.owner.gateway.internal(
            action,
            1,
            lambda: Outcome(
                status="failed",
                data={
                    "error": "transport failure after provider usage",
                    "failure_details": {"usage": {"total_tokens": 15}},
                },
            ),
        )
        usage = goal_usage(
            list(self.store._events(self.store.events())), self.app.state()["goal"]
        )
        self.assertEqual(usage["tokens"], 15)
        self.assertEqual(usage["model_turns"], 1)
        self.assertEqual(usage["by_role"]["parent"]["tokens"], 15)
        self.assertEqual(usage["usage_unknown_calls"], 0)
        next_call = Action(
            id="must-not-send", task=self.fixture.task.id, tool="__propose__"
        )
        with self.assertRaisesRegex(BudgetExceeded, "Goal token budget"):
            self.store.admit(next_call, 1, self.store.policy())
        self.assertFalse(self.requests)

    def test_inactive_goals_hide_stale_progress_and_new_codegen_is_unbounded(self):
        from kernelcat_executor.operations.gateway import Gateway
        from kernelcat_executor.state.application import ModelAdmissionStopped

        # Paused/awaiting/failed/capped history must not be described as active credit.
        self.app.command(
            "accept",
            {
                "input_id": "pause",
                "kind": "control",
                "text": "pause",
                "initiator": "user",
                "channel": "sdk",
            },
        )
        self.app.command("control", {"input_id": "pause", "operation": "pause"})
        self.assertIsNone(model_call_progress(self.store))
        for status in ("awaiting_user", "failed", "budget_limit"):
            with self.subTest(status=status):
                self.app.command(
                    "goal_outcome", {"status": status, "reason": "fixture"}
                )
                self.assertNotIn(
                    "goal_progress",
                    with_model_call_progress(
                        {"goal_progress": {"max_model_turns": 20}}, self.store
                    ),
                )
        # Complete a real project Goal at its ceiling, then accept an ordinary prompt.
        store = AgentJournal(self.fixture.base / "completed-project/events.jsonl")
        store.initialize(None, Policy())
        task = self.fixture.task.model_dump(mode="json")
        store.register_task(task)
        app = ApplicationJournal(store, "completed-project")
        app.command(
            "create",
            {
                "task": task,
                "workspace_mode": "project",
                "goal": {
                    "goal_id": "old-goal",
                    "status": "active",
                    "objective": "Old goal",
                    "on_detach": "pause",
                    "max_model_turns": 20,
                },
            },
        )
        lease = app.command("acquire", {"worker_id": "worker", "mode": "headless"})[
            "lease"
        ]
        store.bind_application_lease(
            "completed-project", "worker", lease["lease_epoch"]
        )
        gateway = Gateway(store, [], self.fixture.task.scope)
        for index in range(20):
            gateway.internal(
                Action(
                    id=f"old-{index}", task=self.fixture.task.id, tool="__propose__"
                ),
                0,
                lambda: Outcome(),
            )
        finish = Action(id="finish-old", task=self.fixture.task.id, tool="finish_goal")
        gateway.internal(finish, 0, lambda: Outcome())
        app.command(
            "goal_outcome",
            {
                "status": "completed",
                "completion_kind": "codegen",
                "action_id": finish.id,
                "summary": "Original fixture completion.",
            },
        )
        self.assertEqual(app.state()["goal"]["status"], "completed")
        self.assertIsNone(model_call_progress(store))
        with self.assertRaises(ModelAdmissionStopped):
            store.admit(
                Action(
                    id="before-new-input", task=self.fixture.task.id, tool="__propose__"
                ),
                1,
                store.policy(),
            )
        app.command(
            "accept",
            {
                "input_id": "ordinary",
                "kind": "prompt",
                "text": "New ordinary task",
                "initiator": "user",
                "channel": "sdk",
            },
        )
        self.assertEqual(app.state()["goal"]["status"], "cleared")
        context = with_model_call_progress(
            {"policy": store.policy().model_dump(mode="json")}, store
        )
        self.assertNotIn("goal_progress", context)
        self.assertIsNone(store.snapshot()["limit"])
        result = propose(
            gateway,
            self.api,
            "ordinary-call",
            self.fixture.task.id,
            "primary",
            context,
            (),
            (),
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
