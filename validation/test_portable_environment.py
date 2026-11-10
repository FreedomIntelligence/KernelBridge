"""Exercise the actual evaluator environment boundary and portable input preparation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for source in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src', ROOT / 'examples/cann']:
    sys.path.insert(0, str(source))
from goal_inputs import local_rsi
from prepare import prepare
from kernelcat_executor.capabilities.process import CommandEvaluator
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.contracts.models import Action


class PortableEnvironmentTests(unittest.TestCase):
    def test_generate_uses_npu_interpreter_and_frozen_environment(self):
        with tempfile.TemporaryDirectory(prefix='kb generate environment ') as temporary:
            root = Path(temporary).resolve()
            venv = root / 'npu python'
            subprocess.run([sys.executable, '-B', '-m', 'venv', '--without-pip', str(venv)], check=True)
            python = venv / 'bin/python'
            packages = next((venv / 'lib').glob('python*/site-packages'))
            (packages / 'npu_only_dependency.py').write_text('marker = "npu venv"\n')
            modules = root / 'installed modules'
            executor = modules / 'kernelcat_executor'
            (executor / 'evaluation/protocols/six_source').mkdir(parents=True)
            for folder in (executor, executor / 'evaluation', executor / 'evaluation/protocols', executor / 'evaluation/protocols/six_source'):
                (folder / '__init__.py').write_text('')
            (executor / 'evaluation/protocols/six_source/config.py').write_text(
                'class Request:\n @classmethod\n def from_dict(cls, value):\n  assert "_runtime" not in value\n  return value\n')
            (executor / 'evaluation/evaluate.py').write_text(
                'import sys,os,npu_only_dependency\n'
                'def run_job(request):\n return {"status":"completed","data":dict('
                'prefix=sys.prefix,python=sys.executable,marker=npu_only_dependency.marker,'
                'vendor=os.environ.get("ASCEND_HOME_PATH"),secret=os.environ.get("DEEPSEEK_API_KEY"))}\n')
            expected = root / 'expected.json'
            expected.write_text(json.dumps({'modules': [{'allowed_package_roots': [(modules / 'kernelcat_worker').as_uri()]}]}))
            profile = root / 'profile.json'
            profile.write_text(json.dumps({'schema_version': 'kernelbridge.cann-profile/v1',
                'python': sys.executable, 'node': shutil.which('node'), 'expected_python_modules': str(expected),
                'npu_python': str(python), 'npu_environment': {'ASCEND_HOME_PATH': '/configured/cann'},
                'config': {'providers': {'primary': {'api_key_env': 'TEST_KEY'}}}}))
            output = root / 'run'
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'not-forwarded'}), patch(
                    'prepare.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
                prepare(ROOT / 'examples/cann/vector-add/task.json', profile, output, 'generate', session_only=True)
            cfg = json.loads((output / 'config.json').read_text())['evaluator']
            workspace = Workspace(root / 'workspace')
            (workspace.root / 'solution.py').write_text('candidate = True\n')
            result = CommandEvaluator(tuple(cfg['argv']), workspace, cfg['parameters'], timeout=30,
                environment={'DEEPSEEK_API_KEY': 'also-not-forwarded'}).execute(Action(
                    id='generate/environment', task='test', tool='evaluate', arguments={'candidate': 'solution.py'}))
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.data['prefix'], str(venv))
            self.assertEqual(result.data['python'], str(python))
            self.assertEqual(result.data['vendor'], '/configured/cann')
            self.assertEqual(result.data['marker'], 'npu venv')
            self.assertIsNone(result.data['secret'])
            self.assertEqual(cfg['env_allowlist'], [])
            appendix = json.loads((output / 'task.json').read_text())['seed_files']['AGENTS.task.md']
            self.assertIn('evaluate(candidate="solution.py")', appendix)
            self.assertNotIn('Use run_experiment', appendix)

    def test_formal_evaluator_preserves_venv_and_frozen_vendor_environment(self):
        with tempfile.TemporaryDirectory(prefix='kb different environment ') as temporary:
            root = Path(temporary).resolve()
            venv = root / 'npu python'
            subprocess.run([sys.executable, '-B', '-m', 'venv', '--without-pip', str(venv)], check=True)
            python = venv / 'bin/python'
            packages = next((venv / 'lib').glob('python*/site-packages'))
            (packages / 'npu_only_dependency.py').write_text('marker = "venv dependency available"\n')
            report = root / 'runtime-observed.json'
            (packages / 'sitecustomize.py').write_text(
                'import os,sys,json,npu_only_dependency\n'
                f'open({str(report)!r},"w").write(json.dumps(dict(prefix=sys.prefix,python=sys.executable,'
                'marker=npu_only_dependency.marker,vendor=os.environ.get("ASCEND_HOME_PATH"),'
                'snapshot=os.environ.get("KERNELCAT_RSI_SOURCE_MANIFEST"),secret=os.environ.get("DEEPSEEK_API_KEY"))))\n')
            output = root / 'run'; (output / 'inputs').mkdir(parents=True)
            reference = output / 'inputs/reference.py'; reference.write_text('x = 1\n')
            profile = {'python': sys.executable, 'npu_python': str(python), '_python_path': str(ROOT / 'source/packages/executor/src'),
                       '_reference': reference.read_text(), 'npu_environment': {'ASCEND_HOME_PATH': '/fixture/cann'}}
            request = {'soc': 'ai_core-Ascend910_9382', 'device': 0, 'reference': str(reference), 'operator': 'add', 'atol': 1e-4, 'rtol': 1e-4}
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'not-forwarded-to-evaluator'}):
                config, text = local_rsi(profile, output, request)
            (output / 'inputs/rsi.json').write_text(text)
            snapshot = root / 'snapshot.json'; snapshot.write_text('{}')
            action = Action(id='test/decompose', task='test', tool='__rsi_adapter__', arguments={
                'operation': 'decompose', 'baseline': [{'path': 'solution.py', 'text': 'x = 1\n'}],
                'keep': [{'path': 'solution.py', 'text': 'x = 2\n'}]})
            evaluator = CommandEvaluator(tuple(config['adapter_argv']), Workspace(root / 'driver'),
                {'type': 'object'}, 1, 30, {'KERNELCAT_RSI_SOURCE_MANIFEST': str(snapshot)}, artifact_root=root / 'receipts')
            result = evaluator.execute(action)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(len(result.data['rsi']['decomposition']['units']), 1)
            observed = json.loads(report.read_text())
            self.assertEqual(observed['prefix'], str(venv))
            self.assertEqual(observed['python'], str(python))
            self.assertEqual(observed['vendor'], '/fixture/cann')
            self.assertEqual(observed['snapshot'], str(snapshot))
            self.assertIsNone(observed['secret'])
            self.assertEqual(observed['marker'], 'venv dependency available')
            self.assertIn(str(venv / 'pyvenv.cfg'), [p['path'] for p in config['adapter_files']])
            profile['npu_environment']['DEEPSEEK_API_KEY'] = 'invalid'
            with self.assertRaisesRegex(ValueError, 'supported CANN runtime'):
                local_rsi(profile, output, request)

    def test_reference_named_baseline_cannot_overwrite_generated_golden(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); task = root / 'author'
            shutil.copytree(ROOT / 'examples/cann/vector-add', task)
            (task / 'reference.py').write_bytes((task / 'baseline.py').read_bytes())
            author = json.loads((task / 'task.json').read_text()); author['baseline_file'] = 'reference.py'
            (task / 'task.json').write_text(json.dumps(author))
            expected = root / 'expected.json'; expected.write_text(json.dumps({'modules': [{'allowed_package_roots': [(root / 'site/kernelcat_worker').as_uri()]}]}))
            profile = root / 'profile.json'; profile.write_text(json.dumps({'schema_version': 'kernelbridge.cann-profile/v1',
                'python': sys.executable, 'node': shutil.which('node'), 'expected_python_modules': str(expected),
                'config': {'providers': {'primary': {'api_key_env': 'TEST_KEY'}}}}))
            with patch('prepare.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
                prepared = prepare(task / 'task.json', profile, root / 'run', 'generate', session_only=True)
            output = Path(prepared['output'])
            self.assertNotEqual((output / 'inputs/reference.py').read_bytes(), (task / 'baseline.py').read_bytes())
            self.assertEqual((output / 'inputs/source/reference.py').read_bytes(), (task / 'baseline.py').read_bytes())
            compiled = json.loads((output / 'task.json').read_text())
            self.assertEqual((output / 'inputs/reference.py').read_text(), compiled['seed_files']['reference/reference.py'])
