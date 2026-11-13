"""Compiler-interface grounding without candidate templates or device dispatch."""
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
from prepare_npu_campaign import NATIVE_INTERFACE, NATIVE_INTERFACE_VERSION, OPERATOR_GUIDANCE, prepare, scatter_contract
from validation.npu_isolation_fixture import preparation_profile


class NativeInterfaceTest(unittest.TestCase):
    def test_document_records_current_compiler_and_admission_sources(self):
        document = NATIVE_INTERFACE.read_text()
        self.assertIn(NATIVE_INTERFACE_VERSION, document)
        records = re.findall(r'\| `([^`]+)` \| `([a-f0-9]{64})` \|', document)
        self.assertEqual(len(records), 11)
        for relative, expected in records:
            with self.subTest(source=relative):
                self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), expected)

    def test_same_interface_reaches_scatter_and_cube_without_implementation_or_contract_change(self):
        document = NATIVE_INTERFACE.read_text()
        guidance = OPERATOR_GUIDANCE.read_text()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            transport = folder / 'transport.py'
            transport.write_text('raise AssertionError("preparation must not load transport")\n')
            case = json.loads((ROOT / 'examples/cann/cube-matmul/case.json').read_text())
            cube_contract = {
                'task': {'operator': case['native_operator'], 'case': case},
                'oracle': {'kind': 'independent', 'description': 'original CPU FP32 oracle, excluded from timing'},
                'suite': [{'id': 'matmul-fp16-128x128x128'}],
                'measurement': {'scope': 'same_device_npu_eager_abba', 'device': 0, 'warmup': 3, 'repeats': 20,
                                'pairs': 12, 'aggregation': 'paired_block_log_mean', 'require_noise': True,
                                'protocol_id': 'cube-fp16-128-eager-abba-12x20-v1'},
            }
            cube_abi = {'native_operator': 'matmul', 'operator_type': 'MatmulCustom',
                        'model': 'ModelNew.forward(a, b)', 'binding': 'custom_ops_lib.matmul_custom(a, b)'}
            cube_task = folder / 'cube-task.json'
            cube_task.write_text(json.dumps({'goal': 'Implement the declared native operator.',
                                             'contract': cube_contract, 'candidate_abi': cube_abi}))
            npu_profile = folder / 'synthetic-pid-view.json'
            npu_profile.write_text(json.dumps(preparation_profile()))
            for mode in ('max', 'min', 'matmul'):
                with self.subTest(mode=mode):
                    output = folder / mode
                    args = SimpleNamespace(output=output, task=cube_task if mode == 'matmul' else None,
                                           profile=npu_profile, runtime_profile=None, mode=mode, device=0,
                                           transport=transport, remote_root='/unused/test-only', python=Path(sys.executable))
                    with patch('subprocess.Popen', side_effect=AssertionError('preparation must not dispatch')):
                        prepare(args)
                    task = json.loads((output / 'task.json').read_text())
                    workflow = json.loads((output / 'project/workflow-inputs.json').read_text())
                    manifest = json.loads((output / 'clean-input-manifest.json').read_text())
                    self.assertEqual(set(task['files']),
                                     {'ABI.md', 'AGENTS.md', 'AGENTS.task.md', 'contract.json', 'workflow-inputs.json'})
                    self.assertEqual(task['writable'], ['candidate.py'])
                    self.assertEqual(task['readonly'], sorted(task['files']))
                    self.assertTrue(set(task['writable']).isdisjoint(task['readonly']))
                    for path, text in task['files'].items():
                        self.assertEqual(text, (output / 'project' / path).read_text())
                    self.assertEqual(task['files']['AGENTS.md'], guidance)
                    self.assertEqual(manifest['operator_guidance'], {
                        'source': 'examples/cann/instructions/AGENTS.md',
                        'sha256': hashlib.sha256(guidance.encode()).hexdigest()})
                    self.assertFalse((output / 'project/candidate.py').exists())
                    self.assertFalse(manifest['candidate_present'])
                    self.assertIsNone(manifest['seed_implementation'])
                    self.assertEqual(manifest['formal_device_jobs'], 0)
                    self.assertEqual(manifest['application'], {'max_model_turns': None})
                    fragment = json.loads((output / 'profile-fragment.json').read_text())
                    self.assertEqual(fragment['application'], manifest['application'])
                    self.assertEqual(manifest['native_interface'], {
                        'version': NATIVE_INTERFACE_VERSION, 'source': 'examples/cann/native-interface.md',
                        'sha256': hashlib.sha256(document.encode()).hexdigest()})
                    abi_text = task['files']['ABI.md']
                    self.assertEqual(abi_text, (output / 'project/ABI.md').read_text())
                    self.assertTrue(abi_text.endswith(document))
                    header, shared = abi_text.split('# Native six-source interface\n', 1)
                    self.assertEqual('# Native six-source interface\n' + shared, document)
                    abi = json.loads(re.search(r'```json\n(.*?)\n```', header, re.S).group(1))
                    self.assertEqual(abi, workflow['candidate_abi'])
                    expected_contract = copy.deepcopy(cube_contract if mode == 'matmul' else scatter_contract(mode, 0))
                    actual_contract = copy.deepcopy(task['contract'])
                    del actual_contract['oracle']['files']
                    isolation = actual_contract['measurement'].pop('device_isolation')
                    from npu_isolation import policy_digest
                    self.assertTrue(isolation['policy']['required'])
                    self.assertEqual(isolation['policy']['claim'], 'sampled_observed_clear')
                    self.assertEqual(isolation['policy']['scope'], 'cooperative_same_uid_namespace')
                    self.assertEqual(isolation['policy']['logical_device'], actual_contract['measurement']['device'])
                    self.assertEqual(isolation['policy_sha256'], policy_digest(isolation['policy']))
                    frozen_profile = json.loads((output / 'pipeline-tools/config.json').read_text())
                    self.assertEqual(frozen_profile['device_isolation'], isolation)
                    self.assertEqual(actual_contract, expected_contract)
                    tools = json.loads((output / 'business-tools.json').read_text())
                    self.assertEqual(len(tools), 11)
                    self.assertEqual([t['name'] for t in tools if t.get('capture_paths')],
                                     ['pipeline_freeze', 'pipeline_evaluate'])
                    evaluate = next(t for t in tools if t['name'] == 'pipeline_evaluate')
                    self.assertEqual(evaluate['capture_paths'], task['writable'])
                    self.assertTrue(any(Path(pin['path']).name == 'evaluation.py' for pin in evaluate['files']))
                    if mode == 'matmul':
                        self.assertEqual(abi, cube_abi)
                    else:
                        self.assertEqual(abi['binding'], 'custom_ops_lib.run(src, index)')
                        self.assertEqual(abi['native_operator'], 'scatter_' + mode)


if __name__ == '__main__':
    unittest.main()
