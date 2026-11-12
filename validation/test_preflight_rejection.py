"""Real local subprocess preflight protocol, commits, recovery and evidence reads.

No model, network or device is used; dispatch counters are protocol fixtures.
"""
import base64
import copy
import hashlib
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
from preflight import PROTOCOL

DRIVER = r'''import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from preflight import encoded,make_rejection,normalized_contract
kind,mode=sys.argv[2:4]; calls=Path(sys.argv[4]); expected_path=Path(sys.argv[5])
r=json.load(sys.stdin); output=Path(r['output'])
def record(what):
 with calls.open('a') as f:f.write(json.dumps({'kind':what,'action_id':r['action_id']})+'\n')
record(kind)
expected=json.loads(expected_path.read_text());actual=normalized_contract(r)
if kind=='collect' and mode!='collect_refusal' and (actual!=expected or mode not in ('valid','check_unknown','diag_unknown')):
 raise RuntimeError('No original terminal job receipt is available')
if kind=='launch' and mode=='timeout':time.sleep(2)
if kind=='launch' and mode=='truncated':print('{"protocol":');sys.exit()
if kind=='launch' and mode=='no_handle':print(json.dumps({'status':'failed','handle':None}));sys.exit()
if kind=='launch' and mode=='collect_refusal':raise RuntimeError('lost original reply; not evidence of no dispatch')
if kind=='launch' and (actual!=expected or mode not in ('valid','check_unknown','diag_unknown')) or kind=='collect' and mode=='collect_refusal':
 if mode=='request_rewrite':
  r['binding']['revision']+=1;r['wait_seconds']=37
  (output/'request.json').write_bytes(encoded(r)+b'\n')
  intent=output.parent/'launch-intent.json';value=json.loads(intent.read_text());value['binding']=r['binding'];intent.write_bytes(encoded(value)+b'\n')
 value,artifacts=make_rejection(r,expected,actual)
 for name,data in artifacts.items():
  path=output/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
 if mode in ('marker','after_marker'):
  (output/'dispatch-boundary.json').write_text('{}')
 if mode=='after_marker':raise ValueError('contract_mismatch after dispatch boundary')
 if mode=='source_change':(Path(r['source']['root'])/'candidate.py').write_text('changed source')
 if mode=='pin_change':expected_path.write_text(expected_path.read_text()+' ')
 if mode=='request':value['request_sha256']='0'*64
 if mode=='source':value['source_sha256']='0'*64
 if mode=='action':value['action_id']='another-action'
 if mode=='binding':value['binding']={**r['binding'],'revision':1000}
 if mode=='operation':value['operation']='collect'
 if mode=='missing':value.pop('evidence')
 if mode in ('handle','collect_refusal'):value['handle']={'job':'fabricated'}
 if mode=='nonzero':print(json.dumps(value));sys.exit(7)
 if mode=='duplicate':
  text=json.dumps(value)[:-1]+',"protocol":"ordinary","status":"completed","phase":"complete","dispatch_outcome":"started","handle":{"job":"fabricated"}}'
  print(text);sys.exit()
 print(json.dumps(value,ensure_ascii=False));sys.exit()
handle={'job':r['source']['sha256'],'pid':73}
if kind=='launch':
 (output/'dispatch-boundary.json').write_text('{}');record('device_dispatch')
 value={'handle':handle,'phase':'complete','status':'completed','job_status':'completed'}
elif kind=='collect':value={'handle':handle,'phase':'complete','status':'completed','job_status':'completed'}
elif kind=='check':
 if mode=='check_unknown':raise RuntimeError('unknown F result')
 value={'checks':dict.fromkeys(('build','semantics','full'),True),'case_count':len(r['contract']['suite']),
 'case_ids':[c['id'] for c in r['contract']['suite']],'oracle_sha256':r['contract']['oracle']['files']['sha256'],'scope':'protocol_fixture'}
elif kind=='diagnostic':value={'status':'unknown','reason':'original J did not settle'}
else:raise AssertionError(kind)
print(json.dumps(value))
'''


class Case:
    def __init__(self, owner, mode='valid', mismatch=True, opted=True, large=False):
        temp = tempfile.TemporaryDirectory(); owner.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.calls_path = self.root / 'calls.jsonl'
        self.source = self.root / 'candidate.py'; self.source.write_text('kernel fixture bytes\n')
        self.oracle = self.root / 'oracle.py'; self.oracle.write_text('# independent fixture\n')
        self.contract = {'task': {'semantics': 'fixed'}, 'suite': [{'id': 'all'}],
            'oracle': {'kind': 'independent', 'description': 'fixture', 'files': {'oracle.py': str(self.oracle)}},
            'measurement': {'scope': 'protocol_fixture'}}
        expected = copy.deepcopy(self.contract); expected['oracle']['files'] = {'oracle.py': sha(self.oracle)}
        expected_path = self.root / 'expected.json'; expected_path.write_text(json.dumps(expected))
        script = self.root / 'driver.py'; script.write_text(DRIVER)
        pins = {str(p): sha(p) for p in (script, expected_path, HELPERS / 'preflight.py')}
        self.config = {'state': str(self.root / 'workflow'), 'read_roots': [str(self.root)], 'evaluation_enabled': True,
            'drivers': {kind: {'kind': kind, 'argv': [sys.executable, '-B', str(script), str(HELPERS), kind, mode, str(self.calls_path), str(expected_path)],
                'files': dict(pins), 'timeout': .05 if mode == 'timeout' and kind == 'launch' else 5}
                for kind in ('launch', 'collect', 'check', 'diagnostic')}}
        if opted:self.config['drivers']['launch']['preflight_rejection_protocol'] = PROTOCOL
        if mode == 'diag_unknown':self.config['diagnostics'] = {'required': True,'scope':'protocol_fixture','drivers':{'ir':'diagnostic'}}
        declared = copy.deepcopy(self.contract)
        if mismatch:
            declared.update(baseline={'kind':'extra'},candidate_abi={'kind':'extra'})
        if large:
            declared.update({('字段🙂/' + str(i)): '完整差异中文🙂' * 25 for i in range(200)})
        self.bind(declared)

    def call(self, tool, identity=None, **arguments):
        return main(self.config, {'id':identity or uuid.uuid4().hex,'task':'fixture-task','tool':tool,'arguments':arguments})

    def bind(self, contract):
        self.call('pipeline_bind',contract=contract)
        self.call('pipeline_plan',stage='B',record={'family':'fixed'})
        self.call('pipeline_plan',stage='C',record={'hypothesis':'fixture'})

    def evaluate(self, **arguments):
        return self.call('pipeline_evaluate',files={'candidate.py':str(self.source)},origin={'scope':'fixture'},**arguments)

    def frozen(self):
        return self.call('pipeline_freeze',files={'candidate.py':str(self.source)},origin={'scope':'fixture'})

    def body(self, reply):
        data=reply['data']
        return data['result'] if isinstance(data['result'],dict) else json.loads((Path(self.config['state'])/data['run']/'result.json').read_text())['data']

    def calls(self):
        return [json.loads(line)['kind'] for line in self.calls_path.read_text().splitlines()] if self.calls_path.exists() else []

    def read_all(self, read, limit=8192):
        arguments=dict(read['arguments'],limit=limit); data=bytearray(); pages=[]
        while True:
            page=self.call('pipeline_read',**arguments)['data'];pages.append(page)
            data.extend(page['text'].encode() if page['encoding']=='utf-8' else base64.b64decode(page['text']))
            if page['eof']:break
            if page['next_offset']<=page['offset']:raise AssertionError('non-progressing page')
            arguments=page['next_read']['arguments']
        return bytes(data),pages


class PreflightRejectionTests(unittest.TestCase):
    def assert_unknown(self, case):
        state=Pipeline(case.config)
        self.assertEqual(len(state.pending_launches()),1)
        self.assertFalse(state.state['handles']);self.assertFalse(state.state.get('launch_rejections'))
        self.assertIsNone(state.state['check']);self.assertNotIn('device_dispatch',case.calls())
        before=case.calls()
        denied=case.call('pipeline_run',stage='E',driver='launch')
        self.assertEqual(denied['status'],'failed');self.assertEqual(case.calls(),before)
        self.assertEqual(len(Pipeline(case.config).pending_launches()),1)

    def test_evaluate_commits_exact_refusal_reads_and_corrected_same_source_launches_once(self):
        case=Case(self);source_sha=sha(case.source)
        reply=case.evaluate();body=case.body(reply)
        self.assertEqual(reply['status'],'completed');self.assertEqual(body['evaluation_status'],'failed')
        self.assertEqual(body['failure_phase'],'preflight');self.assertEqual(body['dispatch_outcome'],'not_started')
        self.assertEqual([r['stage'] for r in body['stages']],['D','E'])
        self.assertEqual(case.calls(),['launch']);self.assertIsNone(body['original_handle']);self.assertIsNone(body['continuation'])
        self.assertNotIn('recover_run',body)
        self.assertEqual([x['path'] for x in body['feedback']['error']['differences']],['/baseline','/candidate_abi'])
        state=Pipeline(case.config);self.assertEqual(state.pending_launches(),[]);self.assertFalse(state.state['handles'])
        self.assertIsNone(state.state['check']);self.assertIsNone(state.state['pair'])
        self.assertEqual(reply['data']['workflow']['next_actions'][0]['tool'],'pipeline_bind')
        for name,artifact in body['feedback']['artifacts'].items():
            raw,pages=case.read_all(artifact['read'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),artifact['sha256']);self.assertEqual(len(raw),artifact['size_bytes'])
            if name=='driver/stderr.log':self.assertEqual(raw,b'');self.assertTrue(pages[-1]['eof'])
        case.bind(case.contract);done=case.body(case.evaluate());self.assertEqual(done['evaluation_status'],'completed')
        self.assertEqual(sha(case.source),source_sha);self.assertEqual(case.calls().count('device_dispatch'),1)
        self.assertEqual(case.calls().count('check'),1);self.assertNotIn('collect',case.calls());self.assertNotIn('diagnostic',case.calls())
        case.evaluate();self.assertEqual(case.calls().count('device_dispatch'),1)

    def test_legacy_E_is_known_failed_without_creating_handle_or_check(self):
        case=Case(self);case.frozen();reply=case.call('pipeline_run',stage='E',driver='launch')
        self.assertEqual(reply['status'],'failed');self.assertEqual(case.body(reply)['dispatch_outcome'],'not_started')
        self.assertEqual(Pipeline(case.config).pending_launches(),[]);self.assertEqual(case.calls(),['launch'])

    def test_invalid_protocol_or_execution_outcome_cannot_clear_intent(self):
        for mode in ('request','source','action','binding','operation','missing','handle','marker','after_marker',
                     'nonzero','duplicate','truncated','no_handle','timeout','request_rewrite'):
            with self.subTest(mode=mode):
                case=Case(self,mode=mode);reply=case.evaluate()
                self.assertEqual(case.body(reply)['evaluation_status'],'pending')
                self.assert_unknown(case)

    def test_unopted_protocol_and_collector_refusal_with_fabricated_handle_stay_unknown(self):
        case=Case(self,opted=False);case.evaluate();self.assert_unknown(case)
        case=Case(self,mode='collect_refusal');case.frozen()
        first=case.call('pipeline_run',stage='E',driver='launch');self.assertEqual(first['status'],'failed')
        run=Pipeline(case.config).pending_launches()[0]['run']
        collector=case.call('pipeline_run',stage='E',driver='collect',recover_run=run)
        self.assertEqual(collector['status'],'failed');self.assert_unknown(case)

    def test_mutated_source_or_driver_pins_during_refusal_remain_fenced(self):
        for mode in ('source_change','pin_change'):
            with self.subTest(mode=mode):
                case=Case(self,mode=mode);case.frozen();reply=case.call('pipeline_run',stage='E',driver='launch')
                self.assertEqual(reply['status'],'failed');self.assertEqual(len(Pipeline(case.config).pending_launches()),1)
                self.assertFalse(Pipeline(case.config).state.get('launch_rejections'));self.assertNotIn('device_dispatch',case.calls())

    def test_receipt_before_phase_commit_crash_is_unknown_not_a_terminal_sidecar(self):
        case=Case(self);original=Pipeline.finish
        def crash(instance,action,output,status,result,diagnostics=None):
            if result.get('dispatch_outcome')=='not_started':raise KeyboardInterrupt('after refusal before phase commit')
            return original(instance,action,output,status,result,diagnostics)
        with patch.object(Pipeline,'finish',crash),self.assertRaises(KeyboardInterrupt):case.evaluate()
        pending=Pipeline(case.config).pending_launches();self.assertEqual(len(pending),1)
        self.assertTrue((Path(case.config['state'])/pending[0]['run']/'driver/receipt.json').is_file())
        self.assert_unknown(case)

    def test_phase_commit_survives_outer_crash_without_rewriting_original_receipts(self):
        case=Case(self);original=Pipeline.finish
        def crash(instance,action,*args,**kwargs):
            if action['tool']=='pipeline_evaluate':raise KeyboardInterrupt('outer commit did not occur')
            return original(instance,action,*args,**kwargs)
        with patch.object(Pipeline,'finish',crash),self.assertRaises(KeyboardInterrupt):case.evaluate()
        state=Pipeline(case.config);self.assertEqual(state.pending_launches(),[])
        run=state.state['launch_rejections'][0]['run'];folder=Path(case.config['state'])/run
        before={p:p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        self.assertEqual(state.committed_launch_rejection(run)['dispatch_outcome'],'not_started')
        self.assertEqual(before,{p:p.read_bytes() for p in before});self.assertEqual(case.calls(),['launch'])
        # This is local commit proof only; the crashed outer Journal still needs its owner reconciliation.

    def test_committed_tampering_and_in_memory_fake_commit_fail_closed(self):
        case=Case(self);reply=case.evaluate();body=case.body(reply);run=body['stages'][-1]['run'];folder=Path(case.config['state'])/run
        paths=[folder/name for name in ('driver/receipt.json','driver/request.json','launch-intent.json','manifest.json',
              'driver/preflight/differences.json','driver/preflight/actual-contract.json','driver/preflight/expected-contract.json','driver/stdout.log','driver/stderr.log')]
        for path in paths:
            with self.subTest(path=path.name):
                before=path.read_bytes();path.write_bytes(before+b' ')
                try:
                    with self.assertRaises((ValueError,KeyError)):Pipeline(case.config).pending_launches()
                finally:path.write_bytes(before)
        marker=folder/'driver/dispatch-boundary.json';marker.write_text('{}')
        try:
            with self.assertRaises(ValueError):Pipeline(case.config).pending_launches()
        finally:marker.unlink()
        self.assertEqual(Pipeline(case.config).pending_launches(),[])
        orphan=Case(self);original=Pipeline.finish;remember=[]
        def crash(instance,action,*args,**kwargs):
            if instance.state.get('launch_rejections'):
                remember.extend(copy.deepcopy(instance.state['launch_rejections']));raise KeyboardInterrupt()
            return original(instance,action,*args,**kwargs)
        with patch.object(Pipeline,'finish',crash),self.assertRaises(KeyboardInterrupt):orphan.evaluate()
        state=Pipeline(orphan.config);state.state['launch_rejections']=remember
        self.assertEqual(len(state.pending_launches()),1)

    def test_existing_unknown_F_or_J_blocks_new_preflight_or_device_work(self):
        for mode in ('check_unknown','diag_unknown'):
            with self.subTest(mode=mode):
                case=Case(self,mode=mode,mismatch=False);case.evaluate();before=case.calls()
                result=case.body(case.evaluate());self.assertEqual(result['evaluation_status'],'pending')
                blocked=case.call('pipeline_run',stage='E',driver='launch')
                self.assertEqual(blocked['status'],'failed');self.assertEqual(case.calls(),before)

    def test_large_unicode_diff_summary_and_hash_bound_pagination(self):
        case=Case(self,large=True);reply=case.evaluate();body=case.body(reply)
        summary=reply['data']['evaluation_summary'];self.assertEqual(summary['failure_phase'],'preflight')
        self.assertEqual(summary['dispatch_outcome'],'not_started');self.assertEqual(summary['feedback'],body['feedback'])
        self.assertTrue(summary['feedback']['error']['truncated']);self.assertIn('/baseline',summary['error'])
        artifact=body['feedback']['artifacts']['driver/preflight/differences.json']
        raw,pages=case.read_all(artifact['read'],limit=4093)
        self.assertGreater(len(raw),16384);self.assertEqual(hashlib.sha256(raw).hexdigest(),artifact['sha256'])
        self.assertGreater(len(pages),1);self.assertTrue(pages[-1]['eof']);self.assertIsNone(pages[-1]['next_read'])
        self.assertTrue(all('\ufffd' not in page['text'] for page in pages))
        self.assertEqual(len(json.loads(raw)),body['feedback']['error']['difference_count'])
        self.assertEqual(case.calls(),['launch']);self.assertEqual(Pipeline(case.config).pending_launches(),[])


if __name__=='__main__':unittest.main()
