"""Goal accounting and lifecycle at the authoritative Journal boundary."""
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import Action, BudgetExceeded, Conflict, Outcome, Policy
from kernelcat_executor.state.application import ApplicationJournal
from kernelcat_executor.state.journal import Journal
from kernelcat_executor.state.goal_progress import goal_usage, token_count, continuation_has_progress


class GoalProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Journal(Path(self.directory.name) / 'events.jsonl')
        self.store.initialize(None, Policy())
        task = {'id': 'task', 'goal': 'code', 'scope': 'code', 'contract': {}}
        self.store.register_task(task)
        self.app = ApplicationJournal(self.store, 'app')
        self.app.command('create', {'task': task, 'workspace_mode': 'project', 'goal': None})
        self.lease = self.app.command('acquire', {'worker_id': 'worker', 'mode': 'headless'})['lease']
        self.store.bind_application_lease('app', 'worker', self.lease['lease_epoch'])

    def tearDown(self):
        self.directory.cleanup()

    def control(self, operation, **values):
        key = str(len(self.store.events()))
        self.app.command('accept', {'input_id': key, 'text': operation, 'kind': 'control', 'channel': 'sdk', 'initiator': 'user'})
        return self.app.command('control', {'input_id': key, 'operation': operation, **values})

    def model(self, key, tokens):
        action = Action(id=key, task='task', tool='__propose__')
        self.store.admit(action, 1, self.store.policy())
        self.store.started(action)
        self.store.settle(action, Outcome(data={'usage': {'total_tokens': tokens}}), 0)

    def test_explicit_goal_token_budget_includes_child_and_compaction(self):
        self.model('before-goal', 900)
        self.control('set', objective='test', token_budget=20)
        self.model('parent', 8)
        self.model('child', 7)
        self.model('compact', 5)
        with self.assertRaises(BudgetExceeded): self.model('denied', 1)
        usage = goal_usage(list(self.store._events(self.store.events())), self.app.state()['goal'])
        self.assertEqual({key: usage[key] for key in ('model_turns', 'tokens', 'usage_unknown_calls')},
                         {'model_turns': 3, 'tokens': 20, 'usage_unknown_calls': 0})
        self.assertEqual(self.store.snapshot()['reserved'], 0)

    def test_shared_twenty_calls_include_children_compaction_and_review(self):
        self.store = Journal(Path(self.directory.name) / 'capped-events.jsonl')
        self.store.initialize(None, Policy())
        task = {'id': 'task', 'goal': 'code', 'scope': 'code', 'contract': {}}
        self.store.register_task(task)
        self.app = ApplicationJournal(self.store, 'capped')
        self.app.command('create', {'task': task, 'workspace_mode': 'project', 'goal': None,
                                   'goal_defaults': {'max_model_turns': 20}})
        lease = self.app.command('acquire', {'worker_id': 'worker', 'mode': 'headless'})['lease']
        self.store.bind_application_lease('capped', 'worker', lease['lease_epoch'])
        self.control('set', objective='strict trial')
        child = 'task/delegate/worker/leaf'
        self.store.register_task({'id': child, 'goal': 'child', 'scope': 'child', 'contract': {}})
        for index in range(20):
            role = index % 4
            identity = ('task/compact/' if role == 1 else 'task/goal-review/' if role == 3 else 'model-') + str(index)
            action = Action(id=identity, task=child if role == 2 else 'task', tool='__propose__')
            self.store.admit(action, 1, self.store.policy()); self.store.started(action)
            self.store.settle(action, Outcome(data={'usage': {'total_tokens': index + 1}}), 0)
        with self.assertRaises(BudgetExceeded): self.model('twenty-first', 1)
        usage = goal_usage(list(self.store._events(self.store.events())), self.app.state()['goal'])
        self.assertEqual(usage['model_turns'], 20)
        self.assertEqual(usage['tokens'], 210)
        self.assertEqual({role: value['model_turns'] for role, value in usage['by_role'].items()},
                         dict.fromkeys(('parent', 'compact', 'child', 'review'), 5))
        self.assertEqual(sum(value['tokens'] for value in usage['by_role'].values()), 210)

    def test_new_objective_has_new_identity_and_accounting(self):
        first = self.control('set', objective='one', token_budget=1)['goal']
        self.model('one', 5)
        self.app.command('goal_outcome', {'status': 'awaiting_user', 'reason': 'old blocker'})
        second = self.control('set', objective='two', token_budget=None)['goal']
        self.assertNotEqual(first['goal_id'], second['goal_id'])
        self.assertNotIn('reason', second)
        self.model('two', 100)
        self.assertEqual(goal_usage(list(self.store._events(self.store.events())), second)['tokens'], 100)

    def test_clear_without_goal_and_cancel_stale_policy(self):
        self.assertIsNone(self.control('clear')['goal'])
        self.control('set', objective='one')
        self.app.command('accept', {'input_id': 'policy', 'text': 'old objective', 'kind': 'policy', 'channel': 'policy', 'initiator': 'loopx'})
        self.control('set', objective='two')
        self.assertEqual(self.app.state()['inputs']['policy']['status'], 'interrupted')

    def test_old_completion_cannot_finish_replaced_goal(self):
        self.control('set', objective='one')
        action = Action(id='finish-old', task='task', tool='finish_goal')
        self.store.admit(action, 0, self.store.policy())
        self.control('set', objective='two')
        with self.assertRaises(Conflict):
            self.app.command('goal_outcome', {'status': 'completed', 'completion_kind': 'codegen',
                'action_id': action.id, 'summary': 'old objective done'})

    def test_protocol_and_status_reads_are_not_progress(self):
        receipt = {'accepted_seq': 1, 'applied_seq': 2, 'completed_seq': 7}
        events = [{'kind': 'admitted', 'seq': 3, 'action': {'tool': '__propose__'}},
                  {'kind': 'admitted', 'seq': 4, 'action': {'tool': 'get_goal'}}]
        self.assertFalse(continuation_has_progress(events, receipt))
        events.append({'kind': 'admitted', 'seq': 5, 'action': {'tool': 'read', 'id': 'r'}})
        self.assertFalse(continuation_has_progress(events, receipt))
        events.append({'kind': 'settled', 'seq': 6, 'action_id': 'r', 'outcome': {'status': 'completed', 'data': {'text': 'new evidence'}}})
        self.assertTrue(continuation_has_progress(events, receipt))

    def test_provider_usage_normalization(self):
        self.assertEqual(token_count({'total_tokens': 30, 'prompt_tokens': 20, 'completion_tokens': 10}), 30)
        self.assertEqual(token_count({'input_tokens': 5, 'output_tokens': 2, 'cache_read_input_tokens': 10}), 17)

    def test_project_fork_limit_without_implicit_trials(self):
        from kernelcat_worker.application_host import ApplicationHost
        self.control('set', objective='first')
        host = object.__new__(ApplicationHost)
        host.config = SimpleNamespace(workspace_mode='project')
        host.journal = self.app
        host.owner = SimpleNamespace(gateway=SimpleNamespace(store=self.store), rsi=None,
            workspace_policy={'root': self.directory.name, 'writable_paths': [], 'readonly_paths': []})
        host.capture_source = lambda: {'source_sha256': 'a' * 64, 'snapshot_kind': 'conversation_only', 'files': []}
        def state():
            value = self.app.state(); value['goal']['max_forks'] = 1
            return dict(value, pending_effect_ids=[], subagents=[], budget={'limit': None, 'spent': 0, 'reserved': 0})
        host.status = state
        connection = SimpleNamespace(worker=self.lease)
        params = {'operation': 'clone', 'pi_session_id': 'native', 'entry_id': 'entry'}
        host.snapshot(connection, params)
        self.app.command('configuration', {**self.lease, 'operation': 'context_started', 'pi_session_id': 'fork'})
        self.assertFalse(self.app.state()['trials'])
        with self.assertRaisesRegex(Conflict, 'fork ceiling'): host.snapshot(connection, params)
        self.control('set', objective='second')
        host.snapshot(connection, params)  # a new Goal does not inherit spent fork quota


if __name__ == '__main__': unittest.main()
