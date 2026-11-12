"""Fresh Goal review uses original metered evidence and cannot bypass controls."""
import json
from types import SimpleNamespace
import unittest

import test_goal_progress as support
from kernelcat_executor.contracts.messages import AssistantTurn, TextContent, ToolCall
from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, Outcome, canonical
from kernelcat_executor.contracts.requests import ModelRequest
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_worker.application_goal_review import review
from kernelcat_worker.application_host import ApplicationHost
from kernelcat_worker.configuration import GoalReviewConfig


class Reviewer:
    def __init__(self, response): self.response = response; self.requests = []; self.hook = lambda: None
    def prepare_harness(self, harness):
        self.requests.append(harness)
        return ModelRequest(provider='openai', endpoint='https://example.invalid/chat/completions', body_json=canonical(harness))
    def invoke_harness(self, request):
        self.hook()
        if isinstance(self.response, Exception): raise self.response
        return AssistantTurn(content=(TextContent(text=json.dumps(self.response)),), stop_reason='stop', usage={'total_tokens': 12})


class GoalReviewTests(unittest.TestCase):
    setUp = support.GoalProgressTests.setUp
    tearDown = support.GoalProgressTests.tearDown
    control = support.GoalProgressTests.control

    def setup_review(self, response):
        self.control('set', objective='Read the original fixture and verify its value')
        self.app.command('accept', {'input_id': 'work', 'text': 'work', 'kind': 'prompt', 'channel': 'sdk', 'initiator': 'user'})
        self.app.command('transition', {**self.lease, 'input_id': 'work', 'status': 'applied', 'entry_id': 'entry', 'pi_session_id': 'native'})
        action = Action(id='read', task='task', tool='read_file')
        self.store.admit(action, 1, self.store.policy()); self.store.started(action)
        self.store.settle(action, Outcome(data={'text': 'original evidence x=1'}), 0)
        self.app.command('transition', {**self.lease, 'input_id': 'work', 'status': 'completed'})
        self.provider = Reviewer(response)
        self.host = SimpleNamespace(journal=self.app, task=SimpleNamespace(id='task'),
            config=SimpleNamespace(workspace_mode="project", goal_review=GoalReviewConfig(enabled=True)),
            owner=SimpleNamespace(gateway=Gateway(self.store, [], 'test'), proposers={'primary': self.provider}))
        self.connection = SimpleNamespace(worker=self.lease)

    def run_review(self):
        state = self.app.state()
        review(self.host, self.connection, state, state['inputs']['work'])

    def test_fresh_evidence_review_and_original_usage(self):
        self.setup_review({'status': 'met', 'summary': 'The original read proves x=1'})
        self.run_review()
        self.assertEqual(self.app.state()['goal']['status'], 'completed')
        request = self.provider.requests[0]
        self.assertEqual(request['tools'], [])
        evidence = json.loads(request['messages'][0]['content'])
        self.assertEqual(evidence['evidence'][0]['outcome']['data']['text'], 'original evidence x=1')
        self.assertEqual(len(request['messages']), 1)
        self.assertEqual(self.store.snapshot()['spent'], 2)

    def test_continue_is_deduplicated_at_same_input_boundary(self):
        self.setup_review({'status': 'continue', 'summary': 'Need the test result'})
        self.run_review(); self.run_review()
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.app.state()['goal']['status'], 'active')

    def test_blocked_and_invalid_verdict_never_complete(self):
        self.setup_review({'status': 'met', 'summary': ''})
        self.run_review()
        self.assertEqual(self.app.state()['goal']['status'], 'awaiting_user')

    def test_late_verdict_cannot_override_user_pause(self):
        self.setup_review({'status': 'met', 'summary': 'Done'})
        self.provider.hook = lambda: self.control('pause')
        self.run_review()
        self.assertEqual(self.app.state()['goal']['status'], 'paused')

    def test_late_verdict_cannot_complete_replacement(self):
        self.setup_review({'status': 'met', 'summary': 'Done'})
        self.provider.hook = lambda: self.control('set', objective='a new objective')
        self.run_review()
        self.assertEqual(self.app.state()['goal']['objective'], 'a new objective')
        self.assertEqual(self.app.state()['goal']['status'], 'active')

    def test_unknown_review_remains_fenced(self):
        self.setup_review(TimeoutError('original request uncertain'))
        with self.assertRaises(InDoubt): self.run_review()
        with self.assertRaises(InDoubt): self.run_review()
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.app.state()['goal']['status'], 'active')

    def test_forged_review_completion_is_rejected(self):
        self.setup_review({'status': 'continue', 'summary': 'Not done'})
        self.run_review()
        action = next(key for key in self.store.snapshot()['actions'] if '/goal-review/' in key)
        with self.assertRaises(Conflict):
            self.app.command('goal_outcome', {'status': 'completed', 'completion_kind': 'review', 'action_id': action, 'summary': 'Done'})

    def test_finish_request_cannot_skip_enabled_review(self):
        self.setup_review({'status': 'continue', 'summary': 'Need verification'})
        result = ApplicationHost.finish_codegen_goal(self.host, Action(id='claim', task='task', tool='finish_goal', arguments={'summary': 'I am done'}))
        self.assertTrue(result.data['review_pending'])
        self.assertEqual(self.app.state()['goal']['status'], 'active')


if __name__ == '__main__': unittest.main(verbosity=2)
