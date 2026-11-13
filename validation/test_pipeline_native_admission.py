"""Local subprocess E/F recovery with the real native isolation byte validators.

Synthetic device observations do not execute a model, SSH command or NPU job.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / 'examples/operator_pipeline'
sys.path.insert(0, str(HELPERS))
from pipeline import Pipeline, main, sha
from npu_isolation_fixture import build_fixture, isolation


DRIVER = r'''import copy,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from npu_isolation_fixture import build_fixture,build_admission_fixture,seal,isolation,identity,snapshot
from pipeline import digest
kind=sys.argv[3];base=Path(sys.argv[4]);r=json.load(sys.stdin);out=Path(r['output'])
mode=(base/'control').read_text();calls=base/'calls.jsonl'
with calls.open('a') as f:f.write(json.dumps({'kind':kind,'action_id':r['action_id'],'handle':r.get('handle')})+'\n')
action=(r.get('recovery') or {}).get('action_id',r['action_id'])
if kind=='check':
 value={'checks':dict.fromkeys(('build','semantics','full'),True),'case_count':len(r['contract']['suite']),
  'case_ids':[c['id'] for c in r['contract']['suite']],'oracle_sha256':r['contract']['oracle']['files']['sha256'],'scope':'local_protocol_fixture'}
 print(json.dumps(value));sys.exit()
if kind=='launch':
 (out/'dispatch-boundary.json').write_text(json.dumps({'protocol':'kernelbridge.driver-preflight/v1',
   'operation':'launch','action_id':action,'source_sha256':r['source']['sha256'],'request_sha256':digest(r)}))
source=r['source']['files']['candidate.py']
fx=build_fixture(source_sha256='c'*64 if mode=='wrong_source' else source,
 action_id='foreign-action' if mode=='wrong_action' else action)
if mode=='wrong_pin':fx['request']['supervisor_sha256']='e'*64;seal(fx)
refusing=mode not in ('clear','foreign','coverage')
if refusing:
 reason={'busy':'cooperative_lease_busy','admission_foreign':'foreign'}.get(mode,'observation_unknown')
 fx=build_admission_fixture(reason,source_sha256=fx['intent']['source_sha256'],action_id=fx['intent']['action_id'])
 if mode=='wrong_pin':fx['request']['supervisor_sha256']='e'*64
 if mode=='dispatched_sample':fx['samples'][0]['phase']='running'
 seal(fx)
 _,binding=isolation._bindings(fx['request_raw'],fx['intent'],fx['policy'])
 admission={**fx['admission'],**binding,'dispatched':mode=='dispatched_true',
  'samples_sha256':isolation.digest(fx['samples_raw'])}
 raw={'admission.json':isolation.canonical(admission)+b'\n','isolation-samples.jsonl':fx['samples_raw'],'execution-request.json':fx['request_raw']}
 if mode=='lost_ack' and kind=='launch':
  (base/'lost-original.json').write_text(json.dumps({k:v.hex() for k,v in raw.items()}));sys.exit(7)
 if mode=='lost_ack':raw={k:bytes.fromhex(v) for k,v in json.loads((base/'lost-original.json').read_text()).items()}
 artifacts={}
 for name,data in raw.items():
  relative='admission/'+name;path=out/relative;path.parent.mkdir(exist_ok=True)
  if not (mode=='missing' and name=='admission.json'):path.write_bytes(data)
  artifacts[relative]=hashlib.sha256(data).hexdigest()
 value={'protocol':'kernelbridge.native-admission/v1','status':'not_started','dispatch_outcome':'not_started',
  'dispatched':False,'action_id':action,'source_sha256':r['source']['sha256'],'request_sha256':digest(r),
  'policy_sha256':isolation.policy_digest(fx['policy']),'artifacts':artifacts,
  'error':{'code':'native_admission_not_started','message':admission['reason'] if mode!='wrong_message' else 'Compilation failed'}}
else:
 if kind=='launch':
  with calls.open('a') as f:f.write(json.dumps({'kind':'child_started','action_id':action})+'\n')
 if mode=='coverage':fx['samples'][2]['error']='Fixture observation unavailable during original execution'
 if mode=='foreign':
  row=fx['samples'][2];foreign=identity(500);snap=snapshot([(0,500)])
  row.update(snap);target,domain,processes=isolation._parsed(row,fx['policy']);row.update(target=target,domain=domain,
    processes=[{'reported':processes[0],'identity':foreign,'lineage':[foreign],'lineage_source':'fresh'}])
 seal(fx)
 for name,data in fx['raw'].items():(out/name).write_bytes(data)
 value={'status':'completed','phase':'complete','job_status':'completed','handle':fx['handle'],
  'native_candidate_sha256':source,'remote_result_sha256':fx['result_sha256'],
  'device_isolation':{'schema_version':'kernelbridge.device-isolation-evidence/v1',
    'policy_sha256':isolation.policy_digest(fx['policy']),
    'artifacts':{name:hashlib.sha256(data).hexdigest() for name,data in fx['raw'].items()}}}
print(json.dumps(value))
'''


class Case:
    def __init__(self, owner, mode='reject', opted=True, required=True):
        temp = tempfile.TemporaryDirectory(); owner.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.source = self.root / 'candidate.py'; self.source.write_text('# fixed candidate\n')
        self.oracle = self.root / 'oracle.py'; self.oracle.write_text('# independent fixture oracle\n')
        self.control = self.root / 'control'; self.control.write_text(mode)
        self.script = self.root / 'driver.py'; self.script.write_text(DRIVER)
        fixture = build_fixture(); paths = {name: str(path) for name, path in fixture['code_paths'].items()}
        pins = {str(p): sha(p) for p in (self.script, ROOT / 'validation/npu_isolation_fixture.py',
            HELPERS / 'pipeline.py', HELPERS / 'job_identity.py', *fixture['code_paths'].values())}
        policy = {'policy': fixture['policy'], 'policy_sha256': isolation.policy_digest(fixture['policy'])}
        metadata = {'native_admission_protocol': 'kernelbridge.native-admission/v1',
            'native_admission_validator': paths['isolation_code_sha256'], 'native_admission_pins': paths,
            'native_admission_source_file': 'candidate.py'}
        self.config = {'state': str(self.root / 'workflow'), 'read_roots': [str(self.root)], 'evaluation_enabled': True,
            **({'device_isolation': policy} if required else {}),
            'drivers': {kind: {'kind': kind, 'argv': [sys.executable, '-B', str(self.script), str(ROOT / 'validation'),
                str(HELPERS), kind, str(self.root)], 'files': dict(pins), 'timeout': 10,
                **(metadata if opted and kind in ('launch', 'collect') else {})} for kind in ('launch', 'collect', 'check')}}
        self.call('pipeline_bind', contract={'task': 'protocol fixture', 'suite': [{'id': 'all'}],
            'oracle': {'kind': 'independent', 'description': 'fixed fixture oracle', 'files': {'oracle.py': str(self.oracle)}},
            'measurement': {'device': 0, **({'device_isolation': policy} if required else {})}})
        self.call('pipeline_plan', stage='B', record={'family': 'all'})
        self.call('pipeline_plan', stage='C', record={'hypothesis': 'local protocol fixture'})

    def call(self, tool, **arguments):
        return main(self.config, {'id': uuid.uuid4().hex, 'tool': tool, 'arguments': arguments})

    def body(self, reply):
        data = reply['data']; value = data['result']
        return value if isinstance(value, dict) else json.loads((Path(self.config['state']) / data['run'] / 'result.json').read_text())['data']

    def evaluate(self, **arguments):
        return self.call('pipeline_evaluate', files={'candidate.py': str(self.source)}, origin={'scope': 'fixture'}, **arguments)

    def freeze(self):
        return self.call('pipeline_freeze', files={'candidate.py': str(self.source)}, origin={'scope': 'fixture'})

    def calls(self, kind=None):
        path = self.root / 'calls.jsonl'; rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if kind is None or row['kind'] == kind]


class NativeAdmissionPipelineTests(unittest.TestCase):
    def test_not_started_is_original_native_refusal_and_status_reads_do_not_dispatch(self):
        case = Case(self); reply = case.evaluate(); body = case.body(reply)
        self.assertEqual(body['failure_phase'], 'native_admission', body)
        self.assertEqual(body['evaluation_status'], 'failed'); self.assertEqual(body['dispatch_outcome'], 'not_started')
        self.assertIsNone(body['original_handle']); self.assertIsNone(body['continuation'])
        self.assertEqual(reply['data']['continuation_progress']['state'], 'waiting')
        pipeline = Pipeline(case.config); self.assertEqual(pipeline.pending_launches(), [])
        self.assertEqual(pipeline.state['handles'], []); self.assertIsNone(pipeline.state['check'])
        self.assertIsNone(pipeline.state['pair']); self.assertEqual(len(pipeline.state['launch_rejections']), 1)
        run = body['feedback']['run']; boundary = Path(case.config['state']) / run / 'driver/dispatch-boundary.json'
        original = boundary.read_bytes(); before = case.calls()
        for _ in range(2): case.call('pipeline_status')
        for item in body['feedback']['artifacts'].values():
            read = item['read']; data = case.call(read['tool'], **read['arguments'])['data']
            self.assertEqual(data['sha256'], item['sha256'])
        self.assertEqual(case.calls(), before); self.assertEqual(boundary.read_bytes(), original)
        self.assertFalse(case.calls('child_started')); self.assertFalse(case.calls('check'))

    def test_busy_foreign_and_observer_unknown_keep_distinct_original_reasons(self):
        for mode, reason in (('reject', 'observation_unknown'), ('busy', 'cooperative_lease_busy'), ('admission_foreign', 'foreign')):
            with self.subTest(mode=mode):
                case = Case(self, mode); body = case.body(case.evaluate())
                self.assertEqual(body['failure_phase'], 'native_admission', body)
                self.assertIn(reason, body['feedback']['error']['message'])
                self.assertFalse(case.calls('child_started'))
                self.assertEqual(Pipeline(case.config).pending_launches(), [])

    def test_lost_ack_recovers_original_admission_and_commits_only_once(self):
        case = Case(self, 'lost_ack'); case.freeze()
        first = case.call('pipeline_run', stage='E', driver='launch'); original = first['data']['run']
        self.assertEqual(len(Pipeline(case.config).pending_launches()), 1)
        second = case.call('pipeline_run', stage='E', driver='collect', recover_run=original)
        self.assertEqual(case.body(second)['failure_phase'], 'native_admission', second)
        state = Pipeline(case.config); self.assertEqual(state.pending_launches(), []); self.assertEqual(state.state['handles'], [])
        record = state.state['launch_rejections'][0]
        self.assertEqual(record['run'], original); self.assertEqual(record['receipt_run'], second['data']['run'])
        before = case.calls(); duplicate = case.call('pipeline_run', stage='E', driver='collect', recover_run=original)
        self.assertEqual(duplicate['status'], 'failed'); self.assertEqual(case.calls(), before)
        self.assertEqual(len(Pipeline(case.config).state['launch_rejections']), 1)

    def test_composite_collect_recovers_refusal_without_F_or_relaunch(self):
        case = Case(self, 'lost_ack'); result = case.body(case.evaluate())
        self.assertEqual(result['failure_phase'], 'native_admission', result)
        self.assertEqual([row['kind'] for row in case.calls()], ['launch', 'collect'])
        self.assertEqual(Pipeline(case.config).pending_launches(), [])

    def test_invalid_or_unopted_proof_keeps_original_fence(self):
        for mode in ('missing', 'wrong_source', 'wrong_action', 'wrong_pin', 'dispatched_true', 'dispatched_sample', 'wrong_message'):
            with self.subTest(mode=mode):
                case = Case(self, mode); result = case.body(case.evaluate())
                self.assertEqual(result['evaluation_status'], 'pending', result)
                state = Pipeline(case.config); self.assertEqual(len(state.pending_launches()), 1)
                self.assertFalse(state.state.get('launch_rejections')); self.assertFalse(state.state['handles'])
                before = case.calls(); case.call('pipeline_run', stage='E', driver='launch')
                self.assertEqual(case.calls(), before); self.assertFalse(case.calls('child_started'))
        case = Case(self, opted=False); result = case.body(case.evaluate())
        self.assertEqual(result['evaluation_status'], 'pending'); self.assertEqual(len(Pipeline(case.config).pending_launches()), 1)

    def test_verified_refusal_allows_explicit_same_source_and_changed_source_retry(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                case = Case(self); rejected = case.body(case.evaluate()); original = sha(case.source)
                case.control.write_text('clear')
                if changed: case.source.write_text('# new explicit candidate\n')
                done = case.body(case.evaluate()); self.assertEqual(done['evaluation_status'], 'completed', done)
                self.assertEqual(len(case.calls('child_started')), 1); self.assertEqual(len(case.calls('check')), 1)
                self.assertEqual(sha(case.source) == original, not changed)
                self.assertIsNotNone(Pipeline(case.config).committed_launch_rejection(rejected['feedback']['run']))

    def test_committed_artifact_tampering_cannot_reclassify_original_launch(self):
        case = Case(self); result = case.body(case.evaluate()); run = result['feedback']['run']
        root = Path(case.config['state']) / run
        for name in ('driver/admission/admission.json', 'driver/admission/execution-request.json',
                     'driver/admission/isolation-samples.jsonl', 'driver/dispatch-boundary.json', 'launch-intent.json'):
            with self.subTest(name=name):
                path = root / name; before = path.read_bytes(); path.write_bytes(before + b' ')
                try:
                    with self.assertRaises(ValueError): Pipeline(case.config).pending_launches()
                finally: path.write_bytes(before)
        self.assertEqual(Pipeline(case.config).pending_launches(), [])

    def test_no_start_sidecar_before_commit_does_not_settle_fence(self):
        case = Case(self); original = Pipeline.finish
        def crash(instance, action, *args, **kwargs):
            if instance.state.get('launch_rejections'): raise KeyboardInterrupt('commit not reached')
            return original(instance, action, *args, **kwargs)
        with patch.object(Pipeline, 'finish', crash), self.assertRaises(KeyboardInterrupt): case.evaluate()
        state = Pipeline(case.config); self.assertEqual(len(state.pending_launches()), 1)
        self.assertFalse(state.state.get('launch_rejections')); self.assertFalse(state.state['handles'])

    def test_terminal_nonqualifying_isolation_remeasurement_has_new_E_and_matching_F(self):
        for mode in ('foreign', 'coverage'):
            with self.subTest(mode=mode):
                case = Case(self, mode); first = case.evaluate(); original = Pipeline(case.config).state['handles'][0]
                proof = Pipeline(case.config).terminal_isolation_retry(original)
                self.assertIn(proof['verdict'], ('foreign', 'unknown'))
                self.assertEqual(first['data']['continuation_progress']['state'], 'waiting')
                first_check = Pipeline(case.config).state['check']['receipt']; original_bytes = Path(first_check).read_bytes()
                case.control.write_text('clear'); second = case.body(case.evaluate())
                self.assertEqual(second['evaluation_status'], 'completed', second)
                state = Pipeline(case.config); self.assertEqual(len(state.state['handles']), 2)
                self.assertEqual(len(case.calls('child_started')), 2); self.assertEqual(len(case.calls('check')), 2)
                handles = state.state['handles']; self.assertNotEqual(handles[0]['handle'], handles[1]['handle'])
                self.assertEqual(case.calls('check')[0]['handle'], handles[0]['handle'])
                self.assertEqual(case.calls('check')[1]['handle'], handles[1]['handle'])
                self.assertEqual(Path(first_check).read_bytes(), original_bytes)
                self.assertIsNone(state.terminal_isolation_retry(handles[1]))
                self.assertNotEqual(state.state['check']['receipt'], first_check)

    def test_invalid_terminal_proof_never_unlocks_retry_and_valid_clear_reuses(self):
        case = Case(self, 'clear'); first = case.body(case.evaluate())
        again = case.body(case.evaluate()); self.assertEqual(again['stages'], [])
        self.assertEqual(first['check_action_id'], again['check_action_id'])
        before = case.calls(); direct = case.call('pipeline_run', stage='E', driver='launch')
        self.assertEqual(direct['status'], 'failed'); self.assertEqual(case.calls(), before)
        retained = Pipeline(case.config).state['handles'][0]
        path = Path(retained['collected']).parent / 'execution-exit.json'; original = path.read_bytes(); path.write_bytes(original + b' ')
        try:
            with self.assertRaises(ValueError): Pipeline(case.config).terminal_isolation_retry(retained)
        finally: path.write_bytes(original)
        self.assertEqual(case.calls(), before)

    def test_no_isolation_no_baseline_bootstrap_contract_is_unchanged(self):
        for required in (False, True):
            with self.subTest(required=required):
                case = Case(self, 'clear', required=required)
                self.assertEqual(case.body(case.evaluate())['evaluation_status'], 'completed')
                promoted = case.call('pipeline_promote'); self.assertEqual(promoted['status'], 'completed', promoted)
                self.assertTrue(case.body(promoted)['bootstrap']); self.assertIsNone(case.body(promoted)['speedup'])


if __name__ == '__main__': unittest.main()
