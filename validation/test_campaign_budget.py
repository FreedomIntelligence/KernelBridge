"""Clean preparation inherits optional budgets; it does not invent one."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
from prepare_npu_campaign import prepare
from validation.npu_isolation_fixture import preparation_profile


class CampaignBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.transport = self.root / 'transport.py'
        self.transport.write_text('raise AssertionError("Preparation must not contact a device")\n')
        self.npu_profile = self.root / 'synthetic-pid-view.json'
        self.npu_profile.write_text(json.dumps(preparation_profile()))

    def prepare(self, runtime=None, max_model_turns=None):
        runtime_path = None
        if runtime is not None:
            runtime_path = self.root / 'runtime.json'
            runtime_path.write_text(json.dumps(runtime))
        output = self.root / ('prepared-' + str(len(list(self.root.iterdir()))))
        prepare(SimpleNamespace(output=output, task=None, profile=self.npu_profile, runtime_profile=runtime_path,
            mode='max', device=0, transport=self.transport, remote_root='/uncontacted/task',
            python=Path(sys.executable), max_model_turns=max_model_turns))
        return output

    def test_clean_workspace_does_not_create_a_session_or_model_budget(self):
        for runtime in (None, {}, {'application': None, 'goal': None}):
            with self.subTest(runtime=runtime):
                output = self.prepare(runtime)
                fragment = json.loads((output / 'profile-fragment.json').read_text())
                self.assertNotIn('max_trials', fragment['goal'])
                self.assertEqual(fragment['application'], {'max_model_turns': None})
                manifest = json.loads((output / 'clean-input-manifest.json').read_text())
                self.assertIsNone(manifest['session_history'])
                self.assertFalse(manifest['candidate_present'])
                self.assertEqual(manifest['formal_device_jobs'], 0)
                self.assertEqual(manifest['application'], {'max_model_turns': None})
                if runtime is not None:
                    profile = json.loads((output / 'profile.json').read_text())
                    self.assertEqual(profile['goal'], fragment['goal'])
                    self.assertEqual(profile['application'], {'max_model_turns': None})

    def test_explicit_budgets_and_goal_settings_survive_preparation(self):
        runtime = {'goal': {'enabled': True, 'max_trials': 4, 'complete_on_acceptance': False},
            'application': {'max_model_turns': 80, 'token_budget': 50000, 'max_forks': 2,
                'fork_policy': {'enabled': False, 'minimum_observations': 3, 'cooldown_turns': 3}},
            'config': {'budget': 120}}
        original = json.loads(json.dumps(runtime))
        output = self.prepare(runtime)
        profile = json.loads((output / 'profile.json').read_text())
        fragment = json.loads((output / 'profile-fragment.json').read_text())
        for key in ('goal', 'application'):
            self.assertEqual(profile[key], original[key])
            self.assertEqual(fragment[key], original[key])
        self.assertEqual(profile['config']['budget'], 120)
        self.assertEqual(json.loads((self.root / 'runtime.json').read_text()), original)

    def test_application_trial_limit_and_explicit_null_remain_unmodified(self):
        for limit in (3, None):
            with self.subTest(limit=limit):
                runtime = {'application': {'max_trials': limit, 'max_model_turns': None},
                           'goal': {'max_trials': None}}
                output = self.prepare(runtime)
                profile = json.loads((output / 'profile.json').read_text())
                self.assertEqual(profile['application'], runtime['application'])
                self.assertIsNone(profile['goal']['max_trials'])

    def test_explicit_cli_model_limit_overrides_only_that_limit(self):
        runtime = {'application': {'max_model_turns': 80, 'token_budget': 50000},
                   'goal': {'max_trials': 2}}
        output = self.prepare(runtime, max_model_turns=40)
        profile = json.loads((output / 'profile.json').read_text())
        self.assertEqual(profile['application'], {'max_model_turns': 40, 'token_budget': 50000})
        self.assertEqual(profile['goal']['max_trials'], 2)


if __name__ == '__main__':
    unittest.main()
