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
from diagnostic_policy import assess_diagnostics, diagnostic_reason


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
elif mode in ('check_ok','check_bad'):
 result={'checks':dict.fromkeys(['build','semantics','full'],mode=='check_ok'),'case_count':len(ids),'case_ids':ids,
         'oracle_sha256':r['contract']['oracle']['files']['sha256'],'scope':'protocol_fixture'}
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

    def diagnostics(self, *, mode='ok', required=False, scope='protocol_fixture'):
        path = self.root / ('diagnostic-' + mode + '.py')
        path.write_text('''import hashlib,json,sys,time
from pathlib import Path
r=json.load(sys.stdin);kind=sys.argv[1];mode=sys.argv[2]
with Path(sys.argv[3]).open('a') as stream: stream.write(kind+'\\n')
if mode=='timeout': time.sleep(2)
p=Path(r['output'])/(kind+'.txt');p.write_text('Original fixture artifact for '+r['source']['sha256'])
artifacts={} if mode=='empty' else {p.name:hashlib.sha256(p.read_bytes()).hexdigest()}
print(json.dumps({'kind':kind,'scope':'protocol_fixture','candidate_sha256':r['source']['sha256'],
 'artifacts':artifacts,'status':'unavailable' if mode=='unavailable' else 'completed',
 'reason':'fixture backend unavailable' if mode=='unavailable' else None}))
''')
        for kind in ('ir', 'profiler'):
            self.config['drivers']['diag_' + kind] = {'kind': 'diagnostic',
                'argv': [sys.executable, '-B', str(path), kind, mode, str(self.root / 'diagnostic-count.txt')],
                'files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()},
                'timeout': .5 if mode == 'timeout' else 30}
        self.config['diagnostics'] = {'on_failure': True, 'on_regression': True, 'required': required,
            'scope': scope, 'drivers': {'ir': 'diag_ir', 'profiler': 'diag_profiler'}}

    def test_failed_check_invokes_bound_diagnostics_and_retains_original_handle(self):
        self.freeze(); self.diagnostics(); self.fixture_driver('check_bad', 'check'); self.fixture_driver('launch', 'launch')
        handle = self.call('pipeline_run', stage='E', driver='launch')['result']['result']['handle']
        result = self.call('pipeline_run', stage='F', driver='check_bad')
        reports = result['result']['diagnostics']['reports']
        self.assertEqual([row['status'] for row in reports], ['completed', 'completed'])
        self.assertEqual((self.root / 'diagnostic-count.txt').read_text(), 'ir\nprofiler\n')
        for row in reports:
            request = json.loads(Path(row['receipt']).with_name('request.json').read_text())
            self.assertEqual(request['trigger']['run'], result['run'])
            self.assertEqual(request['handle'], handle)
            self.assertEqual(request['binding']['source'], row['source'])
        self.assertFalse(Pipeline(self.config).state['check']['passed'])
        self.call('pipeline_plan', stage='C', record={'diagnostic_run': result['run'], 'hypothesis': 'Correct the demonstrated contract failure'})

    def test_regression_diagnostics_reuse_original_source_and_new_source_reruns(self):
        self.freeze(); self.baseline(); self.diagnostics()
        self.fixture_driver('check_ok', 'check'); self.fixture_driver('positive', 'measure'); self.fixture_driver('equal', 'measure')
        self.call('pipeline_run', stage='F', driver='check_ok')
        self.call('pipeline_pair', driver='positive')
        self.assertFalse((self.root / 'diagnostic-count.txt').exists())
        first = self.call('pipeline_pair', driver='equal')['result']['diagnostics']['reports']
        second = self.call('pipeline_pair', driver='equal')['result']['diagnostics']['reports']
        self.assertTrue(all(row['reused'] for row in second))
        self.assertEqual([row['receipt'] for row in first], [row['receipt'] for row in second])
        self.assertEqual(len((self.root / 'diagnostic-count.txt').read_text().splitlines()), 2)
        with (self.root / 'campaign/native.cpp').open('a') as stream: stream.write('\n// new candidate\n')
        self.freeze(); self.call('pipeline_run', stage='F', driver='check_ok')
        self.call('pipeline_pair', driver='equal')
        self.assertEqual(len((self.root / 'diagnostic-count.txt').read_text().splitlines()), 4)

    def test_required_actual_cpu_diagnostics_and_artifact_tamper_gate(self):
        self.freeze()
        self.config['diagnostics'] = {'required': True, 'scope': 'local_cpu_diagnostic', 'drivers': {'ir': 'ir', 'profiler': 'profiler'}}
        result = self.check()['result']['diagnostics']['reports']
        self.assertEqual([r['status'] for r in result], ['completed', 'completed'])
        self.assertTrue(self.call('pipeline_promote')['result']['bootstrap'])
        ir = Path(result[0]['receipt']).parent / 'kernel-O2.ll'
        self.assertTrue(ir.exists()); ir.write_text('tampered')
        self.call('pipeline_promote', failed=True)
        repeated = self.check()['result']['diagnostics']['reports']
        self.assertEqual(repeated[0]['status'], 'failed')
        self.assertTrue(repeated[0]['reused'])

    def test_scope_empty_and_unavailable_diagnostics_never_satisfy_required(self):
        self.freeze(); self.fixture_driver('check_ok', 'check')
        for mode, scope, expected in [('ok', 'npu_diagnostic', 'failed'), ('empty', 'protocol_fixture', 'failed'),
                                      ('unavailable', 'protocol_fixture', 'unavailable')]:
            self.diagnostics(mode=mode, required=True, scope=scope)
            reports = self.call('pipeline_run', stage='F', driver='check_ok')['result']['diagnostics']['reports']
            self.assertTrue(all(row['status'] == expected for row in reports), reports)
            self.assertTrue(Pipeline(self.config).state['check']['passed'])
            self.call('pipeline_promote', failed=True)

    def test_missing_driver_is_unavailable_and_policy_validated(self):
        self.freeze(); self.fixture_driver('check_ok', 'check')
        self.config['diagnostics'] = {'required': True, 'scope': 'npu_diagnostic'}
        reports = self.call('pipeline_run', stage='F', driver='check_ok')['result']['diagnostics']['reports']
        self.assertEqual([row['status'] for row in reports], ['unavailable', 'unavailable'])
        self.call('pipeline_promote', failed=True)
        for policy in ({'required': True}, {'scope': 'npu_diagnostic', 'required': 'true'},
                       {'scope': 'npu_diagnostic', 'drivers': {'ir': 'check_ok'}}):
            with self.assertRaises(ValueError): Pipeline(dict(self.config, diagnostics=policy))

    def test_timeout_diagnostic_retains_original_intent_and_never_relaunches(self):
        self.freeze(); self.fixture_driver('check_ok', 'check'); self.diagnostics(mode='timeout', required=True)
        first = self.call('pipeline_run', stage='F', driver='check_ok')['result']['diagnostics']['reports']
        self.assertEqual([row['status'] for row in first], ['unknown', 'unknown'])
        second = self.call('pipeline_run', stage='F', driver='check_ok')['result']['diagnostics']['reports']
        self.assertTrue(all(row['status'] == 'unknown' and row['reused'] for row in second))
        self.assertEqual(len((self.root / 'diagnostic-count.txt').read_text().splitlines()), 2)
        self.call('pipeline_promote', failed=True)

    def test_uncommitted_diagnostic_intent_blocks_implicit_retry(self):
        self.freeze(); self.fixture_driver('check_ok', 'check'); self.diagnostics(required=True)
        pipeline = Pipeline(self.config)
        key = pipeline.diagnostic_key('ir', 'diag_ir', pipeline.current())
        folder = Path(self.config['state']) / ('e' * 64) / 'diagnostic-ir'; folder.mkdir(parents=True)
        (folder / 'diagnostic-intent.json').write_text(json.dumps({'key': key, 'kind': 'ir'}))
        reports = self.call('pipeline_run', stage='F', driver='check_ok')['result']['diagnostics']['reports']
        self.assertEqual(reports[0]['status'], 'unknown')
        self.assertEqual((self.root / 'diagnostic-count.txt').read_text(), 'profiler\n')

    def test_unresolved_check_timeout_blocks_diagnostic_device_execution(self):
        self.freeze(); self.diagnostics(); path = self.root / 'slowcheck.py'; path.write_text('import time; time.sleep(2)')
        self.config['drivers']['slowcheck'] = {'kind': 'check', 'argv': [sys.executable, '-B', str(path)],
            'files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}, 'timeout': .1}
        result = self.call('pipeline_run', stage='F', driver='slowcheck', failed=True)['result']
        self.assertEqual(result['evaluation_outcome'], 'unknown')
        self.assertTrue(all(row['status'] == 'blocked' for row in result['diagnostics']['reports']))
        self.assertFalse((self.root / 'diagnostic-count.txt').exists())

    def test_in_process_diagnostic_assessment_uses_original_artifacts(self):
        source = 'a' * 64
        policy = {'on_failure': True, 'on_regression': True, 'required': False, 'scope': 'protocol_fixture'}
        result = {'status': 'completed', 'correct': True, 'geomean_speedup': .99}
        records = []
        for kind in ('ir', 'profiler'):
            artifact = self.root / (kind + '.txt'); artifact.write_text('original ' + kind)
            records.append({'kind': kind, 'scope': 'protocol_fixture', 'candidate_sha256': source,
                            'artifacts': {artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()}})
        assessment = assess_diagnostics(policy, result, records, self.root, source, binding={'original_result': 'original-job/result.json'}, handle={'pid': 123})
        self.assertEqual(assessment['reason'], 'regression'); self.assertTrue(assessment['satisfied'])
        self.assertEqual(assessment['original_handle'], {'pid': 123})
        records[0]['scope'] = 'npu_diagnostic'
        assessment = assess_diagnostics(policy, result, records, self.root, source)
        self.assertFalse(assessment['satisfied']); self.assertEqual(assessment['reports'][0]['status'], 'failed')
        records[0] = {'kind': 'ir', 'status': 'unavailable', 'reason': 'No device compiler'}
        assessment = assess_diagnostics(policy, dict(result, status='failed'), records, self.root, source)
        self.assertEqual(assessment['reason'], 'failure'); self.assertFalse(assessment['satisfied'])
        records[1]['status'] = 'pending'
        assessment = assess_diagnostics(policy, result, records, self.root, source)
        self.assertEqual(assessment['reports'][1]['status'], 'failed')
        self.assertIsNone(diagnostic_reason(policy, failed=False, regression=False))
        self.assertEqual(diagnostic_reason(dict(policy, required=True), failed=False, regression=False), 'required')
        self.assertIsNone(diagnostic_reason(None, failed=True, regression=True))

    def test_prepared_tool_timeout_covers_pair_and_serial_diagnostics(self):
        profile = json.loads((self.root / 'campaign/pipeline-profile.json').read_text())['operator_pipeline']
        profile['diagnostics'] = {'required': True, 'scope': 'local_cpu_diagnostic', 'drivers': {'ir': 'ir', 'profiler': 'profiler'}}
        profile['drivers']['measure']['timeout'] = 1000
        profile['drivers']['ir']['timeout'] = 1800
        profile['drivers']['profiler']['timeout'] = 1800
        tools = prepare(profile, self.root / 'timed-inputs', sys.executable)
        self.assertTrue(all(tool['timeout'] >= 7630 for tool in tools))
        self.assertTrue((self.root / 'timed-inputs/diagnostic_policy.py').is_file())
        reply = subprocess.run(tools[0]['argv'], input=json.dumps({'id': 'frozen-status', 'tool': 'pipeline_status', 'arguments': {}}),
                               text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(reply.stdout)['data']['result']['diagnostic_policy']['scope'], 'local_cpu_diagnostic')

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
 time.sleep(30)
else:
 original=Path(r['recovery']['output'],'job.json')
 print(json.dumps({'handle':json.loads(original.read_text()),'phase':'collected','scope':'protocol_fixture'}))
''')
        for name, kind in [('slow', 'launch'), ('recover', 'collect')]:
            self.config['drivers'][name] = {'kind': kind,
                'argv': [sys.executable, '-B', str(path), 'launch' if kind == 'launch' else 'collect', str(counter)],
                'files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}, 'timeout': 5 if kind == 'launch' else 10}
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
