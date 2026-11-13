"""Original synthetic isolation bytes through G/H and host E3; no device work."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))
from pipeline import Pipeline
from prepare import prepare
from isolation import verify
from kernelcat_executor.contracts.models import digest
from kernelcat_worker.application_pipeline_acceptance import promoted_receipt
from npu_isolation_fixture import build_fixture, identity, seal, snapshot
from job_identity import wire_handle


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


DRIVER = r'''import base64,hashlib,json,sys
from pathlib import Path
r=json.load(sys.stdin); p=json.loads(Path(sys.argv[1]).read_text()); out=Path(r['output'])
def sha(raw): return hashlib.sha256(raw).hexdigest()
def digest(value): return sha(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode())
if sys.argv[2]=='check':
 result={'checks':dict.fromkeys(('build','semantics','full'),True),'case_count':1,'case_ids':['case'],
         'oracle_sha256':r['contract']['oracle']['files']['sha256'],'scope':'synthetic_protocol_fixture'}
else:
 for name,data in p['raw'].items(): (out/name).write_bytes(base64.b64decode(data))
 index=int(out.name.removeprefix('arm-')); env={'device':'synthetic-no-device','device_index':0}
 m=r['contract']['measurement']; evaluator=next(iter(r['contract']['oracle']['files']['files'].values()))
 binding=dict(r['binding'],oracle=r['contract']['oracle']['files']['sha256'],protocol=m['protocol_id'],environment=env,evaluator=evaluator)
 def cases(null=False):
  return [{'id':'case','input_sha256':'8'*64,'arms':[{'pair':block,'position':pos,'arm':'eager' if null or pos in (0,3) else 'candidate',
     'us':100. if null or pos in (0,3) else 80.} for block in range(3) for pos in range(4)]}]
 noise={'schema_version':'kernelbridge.noise-calibration/v1','protocol':'independent_eager_aaaa',
        'pairs':3,'repeats':2,'warmup':3,'device':0,'source_sha256':p['source_sha256'],'baseline_sha256':p['baseline_sha256'],
        'driver_sha256':evaluator,'environment':env,'cases':cases(True)}
 noise_raw=(json.dumps(noise,indent=2,allow_nan=False)+'\n').encode(); (out/'noise.json').write_bytes(noise_raw)
 calibration=dict(noise,binding=binding,receipt={'path':'/original/synthetic/noise.json','payload_sha256':digest(noise),'sha256':sha(noise_raw)})
 result={'correct':True,'environment':env,'measurement':m,'cases':{'case':[100. if index in (0,3) else 80.]*3},
   'pairing':{'binding':binding,'cases':cases(),'calibration':calibration},'original_arm_index':index,
   'native_candidate_sha256':p['source_sha256'],'remote_result_sha256':p['result_sha256'],'original_handle':p['handle'],
   'device_isolation':p['descriptor']}
print(json.dumps(result,allow_nan=False))
'''


class PipelineIsolationAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.actions = {}
        for name, text in [('candidate.py', 'value = 1\n'), ('baseline.py', 'value = 2\n'), ('oracle.py', 'independent = True\n')]:
            (self.root / name).write_text(text)
        self.source = {'candidate.py': sha((self.root / 'candidate.py').read_bytes())}
        self.proof = build_fixture(source_sha256=self.source['candidate.py'], result_sha256='7' * 64, action_id='original-E')
        self.requirement = {'policy': self.proof['policy'], 'policy_sha256': sha(json.dumps(
            self.proof['policy'], sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode())}
        self.contract = {'task': {'name': 'synthetic isolation protocol'},
            'oracle': {'kind': 'independent', 'description': 'Synthetic independent correctness protocol',
                       'files': {'oracle.py': str(self.root / 'oracle.py')}},
            'measurement': {'device': 0, 'aggregation': 'pooled_arm_median', 'protocol_id': 'synthetic-abba-v1',
                            'pairs': 3, 'repeats': 2, 'warmup': 3, 'require_noise': True,
                            'scope': 'synthetic_protocol_fixture', 'device_isolation': self.requirement},
            'suite': [{'id': 'case', 'family': 'all'}]}

    def tearDown(self):
        self.temp.cleanup()

    def setup_pipeline(self, *, baseline=True, required=True, mutate=None, noise=True):
        proof = copy.deepcopy(self.proof)
        if mutate:
            mutate(proof)
        payload = {'raw': {name: base64.b64encode(raw).decode() for name, raw in proof['raw'].items()},
                   'source_sha256': self.source['candidate.py'], 'result_sha256': proof['result_sha256'],
                   'baseline_sha256': sha((self.root / 'baseline.py').read_bytes()), 'handle': wire_handle(proof['handle']),
                   'descriptor': {'schema_version': 'kernelbridge.device-isolation-evidence/v1',
                       'policy_sha256': self.requirement['policy_sha256'],
                       'artifacts': {name: sha(raw) for name, raw in proof['raw'].items()},
                       'verdict': 'sampled_observed_clear'}}
        (self.root / 'fixture.json').write_text(json.dumps(payload))
        (self.root / 'driver.py').write_text(DRIVER)
        pins = [ROOT / 'examples/scatter' / name for name in ('npu_isolation.py', 'npu_job_supervisor.py', 'npu_enter.py')]
        pins += [ROOT / 'examples/operator_pipeline' / name for name in ('process_isolation.py', 'job_identity.py')]
        pins += [self.root / 'fixture.json', self.root / 'driver.py']
        profile = {'read_roots': [str(self.root)], 'drivers': {
            name: {'kind': name, 'argv': [sys.executable, '-B', str(self.root / 'driver.py'), str(self.root / 'fixture.json'), name],
                   'files': [str(path) for path in pins],
                   **({'isolation_validator': str(ROOT / 'examples/scatter/npu_isolation.py')} if name == 'measure' else {})}
            for name in ('check', 'measure')}}
        if required:
            profile['device_isolation'] = self.requirement
        else:
            self.contract['measurement'].pop('device_isolation')
        self.contract['measurement']['require_noise'] = noise
        prepare(profile, self.root / 'inputs', sys.executable)
        self.config = json.loads((self.root / 'inputs/config.json').read_text())
        self.pipeline = Pipeline(self.config)
        self.call('pipeline_bind', contract=self.contract)
        if baseline:
            self.call('pipeline_baseline', selection='files', files={'baseline.py': str(self.root / 'baseline.py')})
        self.call('pipeline_plan', stage='C', record={'hypothesis': 'Synthetic protocol fixture only'})
        self.call('pipeline_freeze', files={'candidate.py': str(self.root / 'candidate.py')}, origin={'scope': 'synthetic'})
        self.fid, f = self.call('pipeline_run', stage='F', driver='check')
        self.assertEqual(f['status'], 'completed', f)

    def call(self, tool, **arguments):
        identity = uuid.uuid4().hex
        action = {'id': identity, 'task': 'fixture-task', 'tool': tool, 'arguments': arguments}
        outcome = self.pipeline.apply(action)
        self.actions[identity] = {'action': action, 'terminal': True, 'outcome': copy.deepcopy(outcome)}
        self.pipeline = Pipeline(self.config)
        return identity, outcome

    def pair(self):
        self.gid, g = self.call('pipeline_pair', driver='measure')
        return g

    def host(self):
        state = self.pipeline.state
        files = [{'path': 'candidate.py', 'sha256': self.source['candidate.py'],
                  'text': (self.root / 'candidate.py').read_text(), 'mode': 0o644}]
        source_sha = digest(files)
        checkpoints = self.root / 'host/checkpoints'; checkpoints.mkdir(parents=True, exist_ok=True)
        (checkpoints / (source_sha + '.json')).write_text(json.dumps({'source_sha256': source_sha, 'files': files}))
        contract = copy.deepcopy(state['contract']); contract['oracle']['files'] = contract['oracle']['files']['files']
        self.declared = {'pipeline_contract': contract, 'baseline_files': state['baseline']['files'] if state['baseline'] else None,
                         'source_paths': ['candidate.py'], 'scope': 'synthetic_protocol_fixture'}
        self.contexts = {key: {'task_version': 1, 'evaluator_sha256': 'fixture-evaluator', 'source_sha256': source_sha}
                         for key in self.actions}
        self.host_state = {'task_version': 1}
        self.runtime = SimpleNamespace(task=SimpleNamespace(id='fixture-task'),
            owner=SimpleNamespace(gateway=SimpleNamespace(store=SimpleNamespace(path=self.root / 'host/events.jsonl'))),
            evaluator={'sha256': 'fixture-evaluator', 'files': {str(path): sha(path.read_bytes())
                       for path in (self.root / 'inputs').iterdir() if path.is_file()}})

    def accepted(self):
        return promoted_receipt(self.runtime, self.host_state, self.declared, self.hid,
                                self.actions[self.hid], self.actions, self.contexts)

    def test_real_validator_qualification_H_and_host_E3(self):
        def legacy_stamp(proof):
            proof['handle']['started_ns'] = int(proof['handle']['started_ns'])
            seal(proof)
        self.setup_pipeline(mutate=legacy_stamp)
        g = self.pair(); self.assertEqual(g['status'], 'completed', g)
        self.assertTrue(self.pipeline.state['pair']['qualification']['qualified'])
        self.hid, h = self.call('pipeline_promote'); self.assertEqual(h['status'], 'completed', h)
        self.host(); accepted = self.accepted(); self.assertIsNotNone(accepted)
        self.assertEqual(accepted['device_isolation']['verdict'], 'sampled_observed_clear')
        self.assertEqual(accepted['evidence_tier'], 'E3')
        self.assertEqual(accepted['device_isolation']['scope'], 'cooperative_same_uid_namespace')
        promoted = self.actions[self.hid]['outcome']['data']['promotion_evidence']
        requirement = promoted['contract']['measurement'].pop('device_isolation')
        self.assertIsNone(self.accepted())
        promoted['contract']['measurement']['device_isolation'] = requirement
        original = Path(self.pipeline.state['pair']['receipts'][0]).with_name('isolation-samples.jsonl')
        original.write_bytes(original.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            self.accepted()
        _, h2 = self.call('pipeline_promote'); self.assertEqual(h2['status'], 'failed')
        self.assertTrue(self.pipeline.state['check']['passed'])

    def test_claimed_clear_unknown_and_missing_do_not_qualify_F_survives(self):
        def unknown(proof):
            rows = [json.loads(line) for line in proof['raw']['isolation-samples.jsonl'].splitlines()]
            rows[2]['error'] = 'synthetic observer unavailable'
            samples = b''.join((json.dumps(row) + '\n').encode() for row in rows)
            receipt = json.loads(proof['raw']['isolation-receipt.json']); receipt['samples_sha256'] = sha(samples)
            proof['raw']['isolation-samples.jsonl'] = samples
            proof['raw']['isolation-receipt.json'] = json.dumps(receipt).encode()
        self.setup_pipeline(mutate=unknown)
        self.assertEqual(self.pair()['status'], 'failed')
        self.assertTrue(self.pipeline.state['check']['passed'])
        self.assertIsNone(self.pipeline.state['pair'])
        _, h = self.call('pipeline_promote'); self.assertEqual(h['status'], 'failed')

    def test_missing_original_receipt_rejects_claimed_clear(self):
        self.setup_pipeline(mutate=lambda proof: proof['raw'].pop('isolation-receipt.json'))
        self.assertEqual(self.pair()['status'], 'failed')
        self.assertTrue(self.pipeline.state['check']['passed'])

    def test_model_cannot_strip_frozen_policy(self):
        self.setup_pipeline()
        before = copy.deepcopy(self.pipeline.state)
        def execution_artifacts():
            return {str(path.relative_to(self.pipeline.root)): sha(path.read_bytes())
                    for path in self.pipeline.root.rglob('*') if path.is_file()
                    and ('driver' in path.parts or path.name.endswith('-intent.json'))}
        before_execution = execution_artifacts()
        changed = copy.deepcopy(self.contract); changed['measurement'].pop('device_isolation')
        _, rejected = self.call('pipeline_bind', contract=changed)
        self.assertEqual(rejected['status'], 'failed', rejected)
        self.assertIn('/measurement/device_isolation', rejected['data']['result']['error'])
        after = self.pipeline.state
        self.assertEqual({key: value for key, value in after.items() if key != 'receipts'},
                         {key: value for key, value in before.items() if key != 'receipts'})
        self.assertEqual(after['receipts'][:-1], before['receipts'])
        self.assertEqual(after['receipts'][-1]['tool'], 'pipeline_bind')
        self.assertEqual(after['receipts'][-1]['status'], 'failed')
        self.assertEqual(execution_artifacts(), before_execution)

    def test_without_baseline_correctness_bootstrap_is_not_E3(self):
        self.setup_pipeline(baseline=False)
        self.hid, h = self.call('pipeline_promote'); self.assertEqual(h['status'], 'completed', h)
        self.assertTrue(h['data']['result']['bootstrap']); self.assertIsNone(h['data']['result']['speedup'])
        self.host(); self.assertIsNone(self.accepted())

    def test_dropping_required_baseline_cannot_forge_host_E3(self):
        self.setup_pipeline()
        baseline = copy.deepcopy(self.pipeline.state['baseline']['files'])
        self.call('pipeline_baseline', selection='none')
        self.call('pipeline_run', stage='F', driver='check')
        self.hid, h = self.call('pipeline_promote'); self.assertEqual(h['status'], 'completed', h)
        self.host(); self.declared['baseline_files'] = baseline
        self.assertIsNone(self.accepted())

    def test_old_profile_without_policy_remains_compatible(self):
        self.setup_pipeline(required=False)
        self.assertEqual(self.pair()['status'], 'completed')
        self.hid, h = self.call('pipeline_promote'); self.assertEqual(h['status'], 'completed')
        self.host(); self.assertIsNotNone(self.accepted())
        self.assertNotIn('device_isolation', self.accepted())

    def test_isolation_without_noise_retains_proof_but_not_E3(self):
        self.setup_pipeline(noise=False)
        self.assertEqual(self.pair()['status'], 'completed')
        self.assertEqual(set(self.pipeline.state['pair']['qualification']), {'device_isolation'})
        self.hid, h = self.call('pipeline_promote'); self.assertEqual(h['status'], 'completed')
        self.host(); self.assertIsNone(self.accepted())

    def test_real_validator_rejects_foreign_gap_code_and_result_changes(self):
        self.setup_pipeline()
        def foreign(proof):
            row = proof['samples'][2]
            row.update(snapshot([(0, 999)]))
            row['processes'] = [{'reported': {'board': 0, 'chip': 0, 'reported_pid': 999},
                                 'identity': identity(999), 'lineage': None, 'lineage_source': 'fresh'}]
        def gap(proof):
            proof['samples'][2]['started_mono_ns'] = '30000000000'
            proof['samples'][2]['ended_mono_ns'] = '30010000000'
        changes = {'foreign': foreign, 'window_gap': gap,
                   'source': lambda p: p['intent'].__setitem__('source_sha256', '0' * 64),
                   'device': lambda p: p['intent'].__setitem__('device', 1),
                   'handle': lambda p: p['handle'].__setitem__('pid', 201),
                   'result': lambda p: p['receipt'].__setitem__('result_sha256', '0' * 64)}
        for field in ('supervisor_sha256', 'isolation_code_sha256', 'process_isolation_code_sha256', 'npu_enter_code_sha256'):
            changes[field] = lambda p, field=field: p['request'].__setitem__(field, '0' * 64)
        for name, mutate in changes.items():
            with self.subTest(name=name):
                proof = copy.deepcopy(self.proof); mutate(proof); seal(proof)
                root = self.root / name; root.mkdir()
                descriptor = {'schema_version': 'kernelbridge.device-isolation-evidence/v1',
                              'policy_sha256': self.requirement['policy_sha256'],
                              'artifacts': {name: sha(raw) for name, raw in proof['raw'].items()},
                              'verdict': 'sampled_observed_clear'}
                rows = []
                for position in range(4):
                    folder = root / ('arm-' + str(position)); folder.mkdir()
                    for artifact, raw in proof['raw'].items(): (folder / artifact).write_bytes(raw)
                    rows.append({'device_isolation': descriptor, 'native_candidate_sha256': self.source['candidate.py'],
                                 'original_handle': wire_handle(proof['handle']), 'remote_result_sha256': self.proof['result_sha256']})
                with self.assertRaises(ValueError):
                    verify(rows, self.contract['measurement'], self.source, run_folder=root,
                           driver=self.config['drivers']['measure'], configured=self.requirement)


if __name__ == '__main__':
    unittest.main()
