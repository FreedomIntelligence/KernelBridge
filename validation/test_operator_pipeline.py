"""Local executable A-J acceptance, including the no-Torch bootstrap path."""
import ctypes
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
from pipeline import main, Pipeline
from prepare import prepare


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


class OperatorPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        sys.path.insert(0, str(ROOT / 'examples/scatter'))
        scatter = load(ROOT / 'examples/scatter/prepare.py', 'scatter_prepare')
        scatter.prepare(self.root / 'campaign', sys.executable)
        profile = json.loads((self.root / 'campaign/pipeline-profile.json').read_text())['operator_pipeline']
        prepare(profile, self.root / 'inputs', sys.executable)
        self.config = json.loads((self.root / 'inputs/config.json').read_text())
        self.contract = json.loads((self.root / 'campaign/contract.json').read_text())
        self.call('pipeline_bind', contract=self.contract)

    def tearDown(self): self.temp.cleanup()

    def call(self, tool, failed=False, **arguments):
        result = main(self.config, {'id': uuid.uuid4().hex, 'tool': tool, 'arguments': arguments})
        self.assertEqual(result['status'], 'failed' if failed else 'completed', result)
        return result['data']

    def freeze(self):
        self.call('pipeline_plan', stage='B', record={'families': ['empty', 'sparse', 'collision', 'wide']})
        self.call('pipeline_plan', stage='C', record={'hypothesis': 'Scatter each row once; independent grouped oracle'})
        self.call('pipeline_freeze', files={'native.cpp': str(self.root / 'campaign/native.cpp')}, origin={'task': 'local fixture'})

    def check(self): return self.call('pipeline_run', stage='F', driver='check')

    def fixture_driver(self, name, kind):
        path = self.root / (name + '.py')
        path.write_text('''import json,sys
r=json.load(sys.stdin); ids=[c['id'] for c in r['contract']['suite']]
mode=sys.argv[1]
if mode=='launch': result={'handle': {'job': 'original-123', 'pid': 123, 'log': 'original.log'}}
elif mode=='collect': result={'original': r['handle'], 'phase': 'complete'}
elif mode=='calibrate': result={'receipt': 'local-fixture-receipt', 'scope': 'protocol_fixture', 'official_submission': False}
else:
 value=100 if r['role']=='A' or mode=='equal' else 99
 result={'correct': True, 'environment': {'scope': 'protocol_fixture'}, 'measurement': r['contract']['measurement'],
         'cases': {case:[value,value,value] for case in ids}}
 if mode=='partial': result['cases'].pop(ids[-1])
print(json.dumps(result))
''')
        self.config['drivers'][name] = {'kind': kind, 'argv': [sys.executable, '-B', str(path), name],
            'files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}, 'timeout': 30}
        return path

    def baseline(self):
        self.call('pipeline_baseline', selection='files', files={'native.cpp': str(self.root / 'campaign/native.cpp')})

    def test_protocol_positive_pair_promotes_without_predicted_gain_gate(self):
        self.freeze(); self.baseline(); self.check(); self.fixture_driver('positive', 'measure')
        pair = self.call('pipeline_pair', driver='positive')['result']
        self.assertAlmostEqual(pair['speedup'], 100 / 99)
        roles = [json.loads(Path(p).with_name('request.json').read_text())['role'] for p in pair['receipts']]
        self.assertEqual(roles, list('ABBA'))
        self.assertFalse(self.call('pipeline_promote')['result']['bootstrap'])
        self.fixture_driver('calibrate', 'calibrate')
        result = self.call('pipeline_run', stage='I', driver='calibrate')['result']['result']
        self.assertFalse(result['official_submission'])
        self.call('pipeline_archive', knowledge={'scope': 'protocol fixture, not a measured optimization'})
        self.call('pipeline_plan', stage='B', record={'next': 'new workload family'})

    def test_partial_or_nonpositive_pair_cannot_promote(self):
        self.freeze(); self.baseline(); self.check()
        self.fixture_driver('partial', 'measure'); self.call('pipeline_pair', driver='partial', failed=True)
        self.call('pipeline_promote', failed=True)
        self.fixture_driver('equal', 'measure'); self.call('pipeline_pair', driver='equal')
        self.call('pipeline_promote', failed=True)

    def test_original_handle_collection_and_driver_pin(self):
        self.freeze(); self.fixture_driver('launch', 'launch'); self.fixture_driver('collect', 'collect')
        launch = self.call('pipeline_run', stage='E', driver='launch')['result']['result']
        handle = launch['handle']
        original = Pipeline(self.config).state['candidate']['sha256']
        self.call('pipeline_bind', contract=dict(self.contract, task='a later independent task'))
        self.call('pipeline_freeze', files={'native.cpp': str(self.root / 'campaign/native.cpp')}, origin={}, failed=True)
        self.call('pipeline_run', stage='E', driver='collect', handle={'job': 'invented'}, failed=True)
        receipt = self.call('pipeline_run', stage='E', driver='collect', handle=handle)['result']
        collected = receipt['result']
        self.assertEqual(collected['original'], handle)
        self.assertEqual(receipt['source'], original)
        self.assertNotEqual(receipt['contract'], __import__('pipeline').digest(Pipeline(self.config).state['contract']))
        self.freeze()
        script = self.fixture_driver('equal', 'measure'); script.write_text('raise RuntimeError("changed")')
        self.baseline(); self.check(); self.call('pipeline_pair', driver='equal', failed=True)

    def test_no_torch_bootstrap_incumbent_pair_and_archive(self):
        self.freeze(); checked = self.check()['result']
        self.assertEqual(checked['result']['case_count'], 12)
        self.assertTrue(checked['result']['checks']['full'])
        self.assertFalse(checked['result']['device_acceptance'])
        self.assertIsNone(self.call('pipeline_pair', driver='measure')['result']['improvement'])
        promoted = self.call('pipeline_promote')['result']
        self.assertTrue(promoted['bootstrap']); self.assertIsNone(promoted['speedup'])
        archive = self.call('pipeline_archive', knowledge={'mechanism': 'one row scatter'})['result']
        self.assertFalse(archive['published'])
        self.call('pipeline_baseline', selection='incumbent')
        self.call('pipeline_promote', failed=True)  # switching invalidates checks and pairs
        self.check()
        pair = self.call('pipeline_pair', driver='measure')['result']
        self.assertEqual(len(pair['cases']), 12); self.assertEqual(len(pair['receipts']), 4)
        self.assertIn('collision', pair['families'])
        # Source and compilation are real. Equal-source timing noise is never
        # asserted to be an optimization or forcibly promoted by this test.

    def test_ir_profiler_diagnostics_return_to_hypothesis(self):
        self.freeze()
        ir = self.call('pipeline_run', stage='J', driver='ir')
        self.assertIn('kernel-O2.ll', ir['result']['result']['artifacts'])
        self.assertEqual(ir['result']['source'], Pipeline(self.config).state['candidate']['sha256'])
        page = self.call('pipeline_read', run=ir['run'], path='driver/ir.html', limit=100)['text']
        self.assertIn('<', page)
        profile = self.call('pipeline_run', stage='J', driver='profiler')
        self.assertIn('profile.csv', profile['result']['result']['artifacts'])
        self.call('pipeline_plan', stage='C', record={'diagnostic': ir['run'], 'hypothesis': 'Inspect row loop vectorization'})
        self.assertEqual(Pipeline(self.config).state['stage'], 'C')

    def test_no_oracle_no_promotion_and_partial_suite_rejected(self):
        self.freeze(); self.call('pipeline_promote', failed=True)
        invalid = dict(self.contract, oracle={'kind': 'reference', 'description': 'none'})
        self.call('pipeline_bind', contract=invalid, failed=True)
        self.assertEqual(Pipeline(self.config).state['stage'], 'D')

    def test_source_and_artifact_tamper(self):
        self.freeze(); checked = self.check()
        candidate = Pipeline(self.config).state['candidate']
        (Path(candidate['root']) / 'native.cpp').write_text('changed')
        self.call('pipeline_promote', failed=True)
        manifest = Path(self.config['state']) / checked['run'] / 'manifest.json'
        manifest.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            self.call('pipeline_read', run=checked['run'], path='result.json')

    def test_failed_launch_is_retained_and_never_redispatched_by_identity(self):
        self.freeze()
        action = {'id': 'same-original', 'tool': 'pipeline_run', 'arguments': {'stage': 'E', 'driver': 'unconfigured'}}
        first = main(self.config, action); self.assertEqual(first['status'], 'failed')
        with self.assertRaises(FileExistsError): main(self.config, action)
        self.call('pipeline_run', stage='F', driver='measure', failed=True)

    def test_launch_timeout_blocks_new_identity_and_collect_recovers_original(self):
        self.freeze()
        path = self.root / 'recoverable.py'; counter = self.root / 'dispatches.txt'
        path.write_text('''import json,sys,time
from pathlib import Path
r=json.load(sys.stdin)
if sys.argv[1]=='launch':
 with Path(sys.argv[2]).open('a') as f: f.write('dispatch\\n')
 Path(r['output'],'job.json').write_text(json.dumps({'job':'original-before-timeout'}))
 time.sleep(5)
else:
 original=Path(r['recovery']['output'],'job.json')
 print(json.dumps({'handle':json.loads(original.read_text()),'phase':'collected','scope':'protocol_fixture'}))
''')
        for name, kind in [('slow', 'launch'), ('recover', 'collect')]:
            self.config['drivers'][name] = {'kind': kind,
                'argv': [sys.executable, '-B', str(path), 'launch' if kind == 'launch' else 'collect', str(counter)],
                'files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}, 'timeout': 0.5 if kind == 'launch' else 10}
        first = self.call('pipeline_run', stage='E', driver='slow', failed=True)
        self.assertEqual(first['result']['launch_outcome'], 'unknown')
        original = Pipeline(self.config).state['candidate']['sha256']
        self.assertEqual(self.call('pipeline_status')['result']['pending_launches'][0]['run'], first['run'])
        self.call('pipeline_bind', contract=dict(self.contract, task='changed after lost response'))
        self.freeze()
        self.call('pipeline_run', stage='E', driver='slow', failed=True)
        self.assertEqual(counter.read_text(), 'dispatch\n')
        recovered = self.call('pipeline_run', stage='E', driver='recover', recover_run=first['run'])['result']
        self.assertEqual(recovered['source'], original)
        self.assertEqual(recovered['result']['handle'], {'job': 'original-before-timeout'})
        state = self.call('pipeline_status')['result']
        self.assertEqual(state['pending_launches'], [])
        self.assertEqual(state['handles'][0]['launch_run'], first['run'])
        self.assertEqual(counter.read_text(), 'dispatch\n')
        self.call('pipeline_run', stage='E', driver='recover', recover_run=first['run'], failed=True)

    def test_native_invalid_indices_and_nan_rejected(self):
        library = self.root / 'native.so'
        subprocess.run([shutil.which('clang++'), '-O2', '-shared', '-fPIC', str(ROOT / 'examples/scatter/native.cpp'), '-o', str(library)], check=True)
        native = ctypes.CDLL(str(library)).scatter_extrema
        native.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_int64), ctypes.c_int64, ctypes.c_int64, ctypes.c_int64, ctypes.c_int, ctypes.POINTER(ctypes.c_float)]
        out = (ctypes.c_float * 1)(); x = (ctypes.c_float * 1)(2); index = (ctypes.c_int64 * 1)(-1)
        self.assertEqual(native(x, index, 1, 1, 1, 1, out), 2)
        index[0] = 0; x[0] = float('nan'); self.assertEqual(native(x, index, 1, 1, 1, 1, out), 3)

    def test_ascend_tasks_compile_to_frozen_contract(self):
        sys.path.insert(0, str(ROOT / 'examples/cann'))
        from task_bundle import load_bundle
        for name in ('scatter-max', 'scatter-min', 'vector-add', 'cube-matmul'):
            task, reference, request, manifest = load_bundle(ROOT / 'examples/cann' / name / 'task.json')
            compile(reference, 'reference', 'exec')
            compile(task['seed_files']['reference/native-template.py'], 'baseline', 'exec')
            self.assertEqual(request['operator'], task['contract']['native_operator'])
            self.assertIn('baseline.py', manifest['inputs'])


if __name__ == '__main__': unittest.main()
