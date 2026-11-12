"""Prepare real clean-task inputs without model/device calls or seeded candidates."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))
from kernelcat_worker.configuration import BusinessToolConfig

spec = importlib.util.spec_from_file_location('clean_guidance_prepare', ROOT / 'examples/scatter/prepare_npu_campaign.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class OperatorGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kb-guidance-')
        self.root = Path(self.temporary.name).resolve()
        self.transport = self.root / 'transport.py'
        self.transport.write_text('# Preparation fixture only; never executes transport.\n')

    def tearDown(self):
        self.temporary.cleanup()

    def args(self, name='campaign', **overrides):
        return SimpleNamespace(**dict(dict(profile=None, runtime_profile=None, task=None,
            output=self.root / name, mode='max', device=1, transport=self.transport,
            remote_root='/preparation-fixture', python=Path(sys.executable),
            max_model_turns=None, online_research=False), **overrides))

    def test_clean_task_delivers_guidance_without_candidate_or_implicit_budget(self):
        for mode in ('max', 'min'):
            with self.subTest(mode=mode):
                args = self.args(mode, mode=mode)
                prepare.prepare(args)
                authored = json.loads((args.output / 'task.json').read_text())
                manifest = json.loads((args.output / 'clean-input-manifest.json').read_text())
                self.assertEqual(authored['writable'], ['candidate.py'])
                self.assertNotIn('candidate.py', authored['files'])
                self.assertFalse((args.output / 'project/candidate.py').exists())
                for path in ('AGENTS.md', 'AGENTS.task.md', 'ABI.md'):
                    self.assertIn(path, authored['readonly'])
                    self.assertEqual(authored['files'][path], (args.output / 'project' / path).read_text())
                self.assertEqual(manifest['operator_guidance']['sha256'],
                                 hashlib.sha256(authored['files']['AGENTS.md'].encode()).hexdigest())
                self.assertIsNone(manifest['application']['max_model_turns'])
                self.assertEqual(authored['contract']['suite'], prepare.scatter_contract(mode, 1)['suite'])
                self.assertEqual(authored['contract']['measurement'], prepare.scatter_contract(mode, 1)['measurement'])
                tools = json.loads((args.output / 'business-tools.json').read_text())
                self.assertFalse(any(tool['name'].startswith('online_') for tool in tools))
                for tool in tools:
                    BusinessToolConfig.model_validate_json(json.dumps(tool))

    def test_search_is_real_pinned_tools_and_consumed_config_not_a_worker_extra(self):
        runtime = self.root / 'runtime.json'
        runtime.write_text(json.dumps({'online_research': True, 'config': {
            'business_tools': []}, 'application': {'max_model_turns': 40}}))
        args = self.args(runtime_profile=runtime)
        prepare.prepare(args)
        profile = json.loads((args.output / 'profile.json').read_text())
        self.assertNotIn('online_research', profile)
        self.assertNotIn('online_research', profile['config'])
        self.assertEqual(profile['application']['max_model_turns'], 40)
        tools = profile['config']['business_tools']
        self.assertEqual({t['name'] for t in tools if t['name'].startswith('online_')},
                         {'online_search', 'online_fetch', 'online_read'})
        for tool in tools:
            BusinessToolConfig.model_validate_json(json.dumps(tool))
            for pin in tool['files']:
                self.assertEqual(hashlib.sha256(Path(pin['path']).read_bytes()).hexdigest(), pin['sha256'])
        task = json.loads((args.output / 'task.json').read_text())
        self.assertIn('online-research.json', task['readonly'])
        self.assertFalse((args.output / 'online-research/receipts').exists())

    def test_conflicting_research_and_invalid_model_limits_fail_before_output(self):
        runtime = self.root / 'runtime.json'
        runtime.write_text(json.dumps({'online_research': {'provider': 'brave'}}))
        with self.assertRaisesRegex(ValueError, 'Conflicting online_research'):
            prepare.prepare(self.args(runtime_profile=runtime, online_research=True))
        self.assertFalse((self.root / 'campaign').exists())
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'positive integer'):
                prepare.prepare(self.args(max_model_turns=limit))
        self.assertFalse((self.root / 'campaign').exists())


if __name__ == '__main__':
    unittest.main()
