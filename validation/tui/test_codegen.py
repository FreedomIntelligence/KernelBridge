"""Unbounded accounting and opt-in limits at the real Journal boundary."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import Action, BudgetExceeded, Outcome, Policy
from kernelcat_executor.state.application import ApplicationJournal
from kernelcat_executor.state.journal import Journal
from kernelcat_worker.loopx_application import recommend
from kernelcat_worker.configuration import Config


class CodegenBudgetTests(unittest.TestCase):
    def test_completed_goal_survives_late_interrupt_and_new_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Journal(Path(directory) / 'events.jsonl')
            store.initialize(None, Policy())
            task = {'id': 'task', 'goal': 'code', 'contract': {}}
            store.register_task(task)
            app = ApplicationJournal(store, 'app')
            app.command('create', {'task': task, 'workspace_mode': 'project',
                'goal': {'status': 'active', 'goal_id': 'goal', 'on_detach': 'pause'}})
            lease = app.command('acquire', {'worker_id': 'worker', 'mode': 'headless'})['lease']
            store.bind_application_lease('app', 'worker', lease['lease_epoch'])
            action = Action(id='finish', task='task', tool='finish_goal')
            store.admit(action, 0, store.policy())
            store.started(action)
            app.command('goal_outcome', {'status': 'completed', 'completion_kind': 'codegen',
                'action_id': action.id, 'summary': 'actual local checks passed'})
            store.settle(action, Outcome(data={}), 0)
            for operation in ['interrupt', 'pause', 'resume', 'clear', 'interrupt']:
                key = operation + str(len(store.events()))
                app.command('accept', {'input_id': key, 'text': operation, 'kind': 'control', 'channel': 'sdk', 'initiator': 'user'})
                app.command('control', {'input_id': key, 'operation': operation})
                self.assertIn(app.state()['goal']['status'], {'completed', 'cleared'})
                self.assertEqual(app.state()['inputs'][key]['status'], 'completed')

    def test_no_implicit_limits(self):
        self.assertIsNone(Config.model_fields['credits'].default)
        self.assertIsNone(Policy().max_steps)
        with tempfile.TemporaryDirectory() as directory:
            store = Journal(Path(directory) / 'events.jsonl')
            store.initialize(None, Policy())
            store.register_task({'id': 'task'})
            for index in range(3):
                action = Action(id=f'action-{index}', task='task', tool='read')
                store.admit(action, 150, store.policy())
                store.started(action)
                store.settle(action, Outcome(data={}), 0)
            state = store.snapshot()
            self.assertIsNone(state['limit'])
            self.assertEqual(state['spent'], 450)
            self.assertEqual(state['reserved'], 0)

    def test_explicit_budget_still_enforces_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Journal(Path(directory) / 'events.jsonl')
            store.initialize(3, Policy())
            store.register_task({'id': 'task'})
            store.admit(Action(id='a', task='task', tool='read'), 2, store.policy())
            with self.assertRaises(BudgetExceeded):
                store.admit(Action(id='b', task='task', tool='read'), 2, store.policy())
            self.assertEqual(store.snapshot()['reserved'], 2)

    def test_goal_policy_has_no_implicit_trial_or_model_ceiling(self):
        state = {'goal': {'status': 'active', 'max_model_turns': None, 'max_trials': None, 'max_forks': None,
                         'fork_policy': {'enabled': False}}, 'task_version': 1, 'model_turns': 2000,
                 'pending_effect_ids': [], 'calls': {}, 'inputs': {}, 'budget': {'limit': None, 'spent': 5000, 'reserved': 0},
                 'trials': [], 'evidence': [], 'max_trial_turns': None}
        self.assertEqual(recommend(state)['action'], 'continue')
        state['goal']['max_model_turns'] = 100
        self.assertEqual(recommend(state)['action'], 'stop')
        state['goal'] = None
        self.assertEqual(recommend(state)['action'], 'wait')


if __name__ == '__main__':
    unittest.main()
