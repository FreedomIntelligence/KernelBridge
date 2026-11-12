"""Goal budget readouts keep session limits separate from native work."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import Action, BudgetExceeded, Outcome, Policy
from kernelcat_executor.state.application import ApplicationJournal
from kernelcat_executor.state.goal_progress import goal_usage
from kernelcat_executor.state.journal import Journal
from kernelcat_worker.application_host import ApplicationHost
from kernelcat_worker.loopx_application import recommend


class GoalBudgetProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Journal(Path(temporary.name) / 'events.jsonl')
        self.store.initialize(None, Policy())
        task = {'id': 'task', 'goal': 'generic development', 'scope': 'test', 'contract': {}}
        self.store.register_task(task)
        self.app = ApplicationJournal(self.store, 'app')
        self.app.command('create', {'task': task, 'workspace_mode': 'isolated',
            'max_trial_turns': None,
            'goal': {'goal_id': 'goal', 'objective': 'generic development', 'status': 'active',
                     'max_trials': 1, 'max_model_turns': 4, 'token_budget': None}})
        self.lease = self.app.command('acquire', {'worker_id': 'worker', 'mode': 'headless'})['lease']
        self.store.bind_application_lease('app', 'worker', self.lease['lease_epoch'])
        self.app.command('trial', {**self.lease, 'trial_id': 'session-one',
            'source_sha256': 'source', 'pi_session_id': 'native-session', 'start_model_turns': 0})
        self.host = object.__new__(ApplicationHost)
        self.host.status = self.state

    def state(self):
        state = self.app.state()
        state['goal_usage'] = goal_usage(list(self.store._events(self.store.events())), state['goal'])
        state['model_turns'] = state['goal_usage']['model_turns']
        snapshot = self.store.snapshot()
        state['budget'] = {key: snapshot[key] for key in ('limit', 'spent', 'reserved')}
        state['pending_effect_ids'] = [key for key, row in snapshot['actions'].items() if not row.get('terminal')]
        return state

    def model(self, identity, task='task', tokens=5):
        action = Action(id=identity, task=task, tool='__propose__')
        self.store.admit(action, 1, self.store.policy())
        self.store.started(action)
        self.store.settle(action, Outcome(data={'usage': {'total_tokens': tokens}}), 1)

    def test_unconfigured_limits_are_empty_and_readout_does_not_mutate_state(self):
        for values in ({}, dict.fromkeys(('max_trials', 'max_model_turns', 'max_forks', 'token_budget'))):
            with self.subTest(values=values):
                state = self.state()
                state['goal'] = {'goal_id': 'goal', 'status': 'active', **values}
                before = copy.deepcopy(state)
                self.host.status = lambda: state
                result = self.host.get_codegen_goal(None).data
                self.assertEqual(result['limits'], {})
                self.assertEqual(result['usage']['context_sessions'], 1)
                self.assertIn('Absent/null limits are unset', result['limit_semantics'])
                self.assertEqual(state, before)

    def test_policy_session_limit_is_visible_without_a_goal_wide_limit(self):
        state = self.state()
        for key in ('max_trials', 'max_model_turns', 'max_forks', 'token_budget'):
            state['goal'][key] = None
        state['max_trial_turns'] = 10
        state['model_turns'] = 49
        state['trials'][-1]['start_model_turns'] = 40
        before = copy.deepcopy(state)
        self.host.status = lambda: state
        result = self.host.get_codegen_goal(None).data
        self.assertEqual(result['limits'], {'max_trial_turns': {
            'limit': 10, 'unit': 'model_calls', 'used': 9, 'scope': 'current_context_session',
            'source': 'policy.max_steps', 'applies_to': 'current-session continuation',
            'counting': "application model-call records since this Trial's start_model_turns"}})
        self.assertEqual(state, before)
        self.assertEqual(recommend(state)['action'], 'continue')
        state['model_turns'] = 50
        self.assertEqual(recommend(state)['reason'], 'trial_limit')
        self.assertEqual(self.host.get_codegen_goal(None).data['limits']['max_trial_turns']['used'], 10)
        state['max_trial_turns'] = None
        self.assertEqual(self.host.get_codegen_goal(None).data['limits'], {})

    def test_trial_limit_counts_sessions_not_repeated_native_attempts(self):
        before = self.host.get_codegen_goal(None).data
        for index in range(3):
            action = Action(id=f'native-attempt-{index}', task='task', tool='pipeline_evaluate')
            self.store.admit(action, 1, self.store.policy())
            self.store.started(action)
            # Original local receipt fixtures only; no compiler or device work.
            self.store.settle(action, Outcome(status='failed', data={'fixture': True}), 1)
        after = self.host.get_codegen_goal(None).data
        self.assertEqual(after['limits']['max_trials'], before['limits']['max_trials'])
        self.assertEqual(after['limits']['max_trials']['used'], 1)
        self.assertEqual(after['limits']['max_trials']['unit'], 'context_sessions')
        self.assertIn('starting another context session', after['limits']['max_trials']['applies_to'])
        self.assertIn('device attempts do not consume', after['limits']['max_trials']['counting'])
        state = self.state()
        self.assertEqual(recommend(state)['action'], 'continue')
        state['max_trial_turns'] = 0  # Exercise the existing new-session gate.
        self.assertEqual(recommend(state)['reason'], 'budget_limit')
        state['goal']['max_trials'] = 2
        self.assertEqual(recommend(state)['action'], 'reset')

    def test_parent_child_compact_review_share_existing_accounting_and_ceiling(self):
        child = 'task/delegate/leaf'
        self.store.register_task({'id': child, 'goal': 'child', 'scope': 'test', 'contract': {}})
        self.model('parent')
        self.model('child', task=child)
        self.model('task/compact/one')
        self.model('task/goal-review/one')
        result = self.host.get_codegen_goal(None).data
        self.assertEqual(result['usage']['model_turns'], 4)
        self.assertEqual(result['usage']['tokens'], 20)
        self.assertEqual({key: value['model_turns'] for key, value in result['usage']['by_role'].items()},
                         dict.fromkeys(('parent', 'child', 'compact', 'review'), 1))
        self.assertEqual(result['limits']['max_model_turns']['used'], 4)
        self.assertEqual(result['budget']['spent'], 4)
        with self.assertRaises(BudgetExceeded):
            self.model('still-refused', task=child)
        self.assertEqual(self.host.get_codegen_goal(None).data['usage']['model_turns'], 4)

    def test_readout_never_grants_admission_over_other_limits_or_pending_effects(self):
        state = self.state()
        state['pending_effect_ids'] = ['original-unknown']
        state['goal']['token_budget'] = 20
        state['goal_usage']['tokens'] = 20
        state['budget'] = {'limit': 3, 'spent': 2, 'reserved': 1}
        self.host.status = lambda: state
        result = self.host.get_codegen_goal(None).data
        self.assertEqual(result['limits']['credits'], {'limit': 3, 'unit': 'credits', 'used': 2,
            'reserved': 1, 'applies_to': 'shared model and tool admission'})
        self.assertEqual(result['limits']['token_budget']['used'], 20)
        self.assertNotIn('can_continue', result)
        self.assertNotIn('authorized', result)
        self.assertIn('unresolved-effect fences still apply', result['limit_semantics'])
        self.assertEqual(state['pending_effect_ids'], ['original-unknown'])


if __name__ == '__main__':
    unittest.main()
