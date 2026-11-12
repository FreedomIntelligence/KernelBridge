"""Executable D/E/F and interrupted-effect recovery; local protocol fixtures only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
from pipeline import Pipeline, main, sha


DRIVER = '''import json,sys,time
from pathlib import Path
r=json.load(sys.stdin); kind=sys.argv[1]; calls=Path(sys.argv[2]); gate=Path(sys.argv[3])
row={'kind':kind,'id':r['action_id'],'source':r['source']['sha256'],'handle':r.get('handle')}
with calls.open('a') as f: f.write(json.dumps(row)+'\\n')
text=''.join((Path(r['source']['root'])/name).read_text() for name in r['source']['files'])
handle={'job':r['source']['sha256'],'pid':73}
if kind=='launch':
 if 'slow_launch' in text: time.sleep(20)
 result={'handle':handle,'status':'completed','job_status':'completed' if gate.exists() else 'running'}
 if gate.exists(): result['phase']='complete'
elif kind=='collect':
 assert r.get('recovery') or r.get('handle')==handle, 'must recover original identity'
 result={'handle':handle,'status':'completed','phase':'complete' if gate.exists() else 'running','job_status':'completed' if gate.exists() else 'running'}
elif kind=='check':
 if 'slow_check' in text: time.sleep(20)
 good='bad' not in text
 result={'checks':dict.fromkeys(('build','semantics','full'),good),'case_count':len(r['contract']['suite']),
         'case_ids':[c['id'] for c in r['contract']['suite']],'oracle_sha256':r['contract']['oracle']['files']['sha256'],
         'scope':'protocol_fixture','error':None if good else 'candidate.cpp:8: error: fixture compilation failed'}
 (Path(r['output'])/'unicode.txt').write_text('prefix 中文🙂 tail\\n'*1000)
 if 'large' in text: result['context']='retained full original context '*1000
elif kind=='diagnostic': result={'status':'unknown','reason':'original diagnostic not yet settled'}
else: raise AssertionError(kind)
print(json.dumps(result))
'''


class EvaluatePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'candidate.py'; self.source.write_text('good\n')
        self.oracle = self.root / 'oracle.py'; self.oracle.write_text('# independent fixture oracle\n')
        self.calls_path = self.root / 'calls.jsonl'; self.gate = self.root / 'terminal'; self.gate.touch()
        self.driver_path = self.root / 'driver.py'; self.driver_path.write_text(DRIVER)
        self.config = {'state': str(self.root / 'workflow'), 'read_roots': [str(self.root)], 'evaluation_enabled': True,
                       'drivers': {kind: {'kind': kind, 'argv': [sys.executable, '-B', str(self.driver_path), kind, str(self.calls_path), str(self.gate)],
                           'files': {str(self.driver_path): sha(self.driver_path)}, 'timeout': 2}
                           for kind in ('launch', 'collect', 'check')}}
        self.call('pipeline_bind', contract={'task': 'local fixture', 'suite': [{'id': 'all'}],
            'oracle': {'kind': 'independent', 'description': 'fixture checker', 'files': {'oracle.py': str(self.oracle)}},
            'measurement': {'scope': 'protocol_fixture'}})
        self.call('pipeline_plan', stage='B', record={'family': 'one'})
        self.call('pipeline_plan', stage='C', record={'hypothesis': 'fixture candidate'})

    def call(self, tool, *, expected='completed', identity=None, **arguments):
        identity = identity or uuid.uuid4().hex
        reply = main(self.config, {'id': identity, 'task': 'fixture-task', 'tool': tool, 'arguments': arguments})
        self.assertEqual(reply['status'], expected, reply)
        return reply['data']

    def evaluate(self, **overrides):
        return self.call('pipeline_evaluate', files={'candidate.py': str(self.source)}, origin={'scope': 'fixture'}, **overrides)

    def calls(self, kind=None):
        rows = [json.loads(line) for line in self.calls_path.read_text().splitlines()] if self.calls_path.exists() else []
        return [row for row in rows if kind is None or row['kind'] == kind]

    def read(self, run, path, **kw):
        return self.call('pipeline_read', run=run, path=path, **kw)

    def result(self, reply):
        if isinstance(reply['result'], dict): return reply['result']
        return json.loads((Path(self.config['state']) / reply['run'] / 'result.json').read_text())['data']

    def test_success_has_actual_D_E_F_original_F_and_no_implicit_G_H(self):
        reply = self.evaluate(); result = self.result(reply)
        self.assertEqual(result['evaluation_status'], 'completed')
        self.assertEqual([row['stage'] for row in result['stages']], ['D', 'E', 'F'])
        self.assertEqual([row['kind'] for row in self.calls()], ['launch', 'check'])
        state = Pipeline(self.config).state
        self.assertEqual(result['check_action_id'], state['check']['action_id'])
        self.assertEqual(result['check_receipt'], json.loads(Path(state['check']['receipt']).read_text()))
        self.assertIsNone(state['pair']); self.assertIsNone(state['incumbent'])
        self.assertTrue(state['handles'][0]['collected'])

    def test_local_without_launch_and_failed_check_remain_original_F(self):
        self.config['drivers'] = {'check': self.config['drivers']['check']}
        self.source.write_text('bad\n')
        reply = self.evaluate(); result = self.result(reply)
        self.assertEqual(result['evaluation_status'], 'failed')
        self.assertEqual([row['stage'] for row in result['stages']], ['D', 'F'])
        self.assertFalse(Pipeline(self.config).state['check']['passed'])
        self.assertEqual(result['check_receipt']['result']['error'], 'candidate.cpp:8: error: fixture compilation failed')
        self.assertEqual(reply['workflow']['next_actions'][0]['stage'], 'C')

    def test_same_hash_reuses_original_and_changed_hash_executes_one_new_job(self):
        first = self.evaluate(); initial = self.result(first)
        again = self.result(self.evaluate())
        self.assertFalse(again['source_changed']); self.assertEqual(again['stages'], [])
        self.assertEqual(again['check_action_id'], initial['check_action_id'])
        self.assertEqual(len(self.calls('launch')), 1); self.assertEqual(len(self.calls('check')), 1)
        zero_wait = self.result(self.evaluate(wait_seconds=0))
        self.assertEqual(zero_wait['evaluation_status'], 'completed')
        self.assertEqual(zero_wait['check_action_id'], initial['check_action_id'])
        self.assertIn('reused_check', zero_wait)
        self.assertEqual(len(self.calls('launch')), 1); self.assertEqual(len(self.calls('check')), 1)
        self.source.write_text('good new bytes\n')
        changed = self.result(self.evaluate())
        self.assertTrue(changed['source_changed']); self.assertNotEqual(changed['source_sha256'], initial['source_sha256'])
        self.assertEqual(len(self.calls('launch')), 2); self.assertEqual(len(self.calls('check')), 2)

    def test_bounded_pending_resumes_original_handle_without_launching_again(self):
        self.gate.unlink()
        # Establish the original running job before exercising a zero wait;
        # startup and durable freeze latency are not part of this assertion.
        self.call('pipeline_freeze', files={'candidate.py': str(self.source)}, origin={'scope': 'fixture'})
        launched = self.call('pipeline_run', stage='E', driver='launch')
        original_handle = launched['result']['result']['handle']
        self.assertEqual(launched['result']['result']['job_status'], 'running')
        original_launch = self.calls('launch')[0]
        first = self.evaluate(wait_seconds=0); pending = self.result(first)
        self.assertEqual(pending['evaluation_status'], 'pending')
        self.assertEqual(pending['original_handle'], original_handle)
        self.assertFalse(pending['source_changed']); self.assertEqual(pending['stages'], [])
        self.assertEqual(self.calls('launch'), [original_launch]); self.assertEqual(self.calls('check'), [])
        self.gate.touch()
        continuation = pending['continuation']
        self.assertEqual(continuation['arguments']['resume_run'], first['run'])
        resumed = self.result(self.call(continuation['tool'], **{**continuation['arguments'], 'wait_seconds': 2}))
        self.assertEqual(resumed['evaluation_status'], 'completed')
        self.assertEqual(self.calls('launch'), [original_launch]); self.assertEqual(len(self.calls('check')), 1)
        self.assertEqual([row['handle'] for row in self.calls('collect')], [original_handle])
        self.assertEqual(self.calls('check')[0]['handle'], original_handle)
        self.assertEqual({row['source'] for row in self.calls()}, {pending['source_sha256']})
        retained = Pipeline(self.config).state['handles']
        self.assertEqual(len(retained), 1); self.assertEqual(retained[0]['launch_run'], launched['run'])

    def crash_during(self, kind):
        self.source.write_text('slow_' + kind + '\n')
        self.config['drivers'][kind]['timeout'] = 60
        config = self.root / 'config.json'; config.write_text(json.dumps(self.config))
        identity = uuid.uuid4().hex
        action = {'id': identity, 'tool': 'pipeline_evaluate', 'arguments': {
            'files': {'candidate.py': str(self.source)}, 'origin': {'scope': 'fixture'}, 'wait_seconds': 60}}
        process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'examples/operator_pipeline/pipeline.py'), str(config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        process.stdin.write(json.dumps(action).encode()); process.stdin.close()
        try:
            until = time.monotonic() + 5
            while not self.calls(kind) and time.monotonic() < until: time.sleep(.01)
            self.assertTrue(self.calls(kind), process.poll())
        finally:
            os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
            process.stdout.close(); process.stderr.close()
        return hashlib.sha256(identity.encode()).hexdigest()

    def test_crashed_launch_recovers_original_intent_and_never_dispatches_again(self):
        run = self.crash_during('launch')
        self.assertEqual(len(Pipeline(self.config).pending_launches()), 1)
        reply = self.result(self.call('pipeline_evaluate', resume_run=run, wait_seconds=2))
        self.assertEqual(reply['evaluation_status'], 'completed')
        self.assertEqual(len(self.calls('launch')), 1); self.assertEqual(len(self.calls('collect')), 1)

    def test_failed_preflight_with_pending_launch_returns_a_real_original_recovery(self):
        self.crash_during('launch')
        self.source.write_text('replacement bytes must not dispatch\n')
        reply = self.evaluate(); result = self.result(reply)
        self.assertEqual(result['evaluation_status'], 'pending')
        self.assertFalse((Path(self.config['state']) / reply['run'] / 'evaluation-intent.json').exists())
        self.assertEqual(result['continuation']['tool'], 'pipeline_run')
        original = self.call(result['continuation']['tool'], **result['continuation']['arguments'])
        self.assertEqual(original['result']['result']['phase'], 'complete')
        self.assertEqual(len(self.calls('launch')), 1)

    def test_crashed_F_is_fenced_for_composite_and_legacy_driver_entries(self):
        run = self.crash_during('check')
        reply = self.result(self.call('pipeline_evaluate', resume_run=run, wait_seconds=2))
        self.assertEqual(reply['evaluation_status'], 'pending'); self.assertIsNone(reply['continuation'])
        before = len(self.calls())
        for stage, driver in [('E', 'launch'), ('F', 'check')]:
            rejected = self.call('pipeline_run', stage=stage, driver=driver, expected='failed')
            self.assertIn('unresolved', rejected['result']['error'])
        self.assertEqual(len(self.calls()), before)

    def test_orphan_committed_F_resumes_read_only_without_repeating_check(self):
        original_finish = Pipeline.finish
        def crash_outer(instance, action, *args, **kwargs):
            if action['tool'] == 'pipeline_evaluate': raise KeyboardInterrupt('crash before outer commit')
            return original_finish(instance, action, *args, **kwargs)
        identity = uuid.uuid4().hex
        with patch.object(Pipeline, 'finish', crash_outer), self.assertRaises(KeyboardInterrupt):
            self.evaluate(identity=identity)
        run = hashlib.sha256(identity.encode()).hexdigest()
        reply = self.result(self.call('pipeline_evaluate', resume_run=run))
        self.assertEqual(reply['evaluation_status'], 'completed')
        self.assertEqual(len(self.calls('check')), 1)
        descriptor = reply['stages'][0]
        self.assertTrue(descriptor['reused']); self.assertEqual(descriptor['origin_action_id'], identity)
        self.assertTrue(descriptor['action_id'].startswith(identity + '/phase/'))
        self.assertEqual(Pipeline(self.config).state['check']['action_id'], reply['check_action_id'])

    def test_unknown_diagnostic_blocks_legacy_and_new_device_work(self):
        self.config['drivers']['diag'] = {'kind': 'diagnostic', 'argv': [sys.executable, '-B', str(self.driver_path), 'diagnostic', str(self.calls_path), str(self.gate)],
            'files': {str(self.driver_path): sha(self.driver_path)}}
        self.config['diagnostics'] = {'required': True, 'scope': 'protocol_fixture', 'drivers': {'ir': 'diag'}}
        self.evaluate()
        self.assertTrue(Pipeline(self.config).pending_diagnostics())
        before = len(self.calls())
        self.source.write_text('changed good\n')
        self.assertEqual(self.result(self.evaluate())['evaluation_status'], 'pending')
        self.call('pipeline_run', stage='E', driver='launch', expected='failed')
        self.call('pipeline_run', stage='F', driver='check', expected='failed')
        self.assertEqual(len(self.calls()), before)

    def test_large_failed_local_F_keeps_original_error_visible_with_read_locator(self):
        self.config['drivers'] = {'check': self.config['drivers']['check']}
        self.source.write_text('large bad\n')
        reply = self.evaluate(); self.assertIsInstance(reply['result'], str)
        summary = reply['evaluation_summary']
        self.assertEqual(summary['evaluation_status'], 'failed')
        feedback = summary['feedback']
        self.assertEqual(feedback['error_excerpt'], 'candidate.cpp:8: error: fixture compilation failed')
        original = self.result(reply)['check_receipt']
        self.assertEqual(feedback['error_excerpt'], original['result']['error'])
        receipt = feedback['original_receipt']
        self.assertEqual(receipt['sha256'], sha(receipt['path']))
        self.assertEqual(receipt['read']['arguments']['path'], 'driver/receipt.json')

    def test_large_result_keeps_summary_and_hash_bound_read_index_and_UTF8_pages(self):
        self.source.write_text('large\n')
        reply = self.evaluate(); self.assertIsInstance(reply['result'], str)
        summary = reply['evaluation_summary']
        self.assertEqual(summary['evaluation_status'], 'completed'); self.assertEqual(len(summary['stages']), 3)
        result = self.result(reply); stage = result['stages'][-1]
        expected = (Path(self.config['state']) / stage['run'] / 'driver/unicode.txt').read_bytes()
        data = bytearray(); offset = 0
        while True:
            page = self.read(stage['run'], 'driver/unicode.txt', offset=offset, limit=13)
            data.extend(page['text'].encode() if page['encoding'] == 'utf-8' else base64.b64decode(page['text']))
            self.assertEqual(page['size_bytes'], len(expected)); self.assertEqual(page['sha256'], hashlib.sha256(expected).hexdigest())
            self.assertNotIn('\ufffd', page['text']); self.assertGreater(page['next_offset'], offset)
            if page['eof']: self.assertFalse(page['truncated']); self.assertIsNone(page['next_read']); break
            offset = page['next_offset']
        self.assertEqual(bytes(data), expected)
        partial = self.read(stage['run'], 'driver/unicode.txt', offset=8, limit=1)
        self.assertEqual(partial['encoding'], 'base64'); self.assertEqual(base64.b64decode(partial['text']), expected[8:9])
        eof = self.read(stage['run'], 'driver/unicode.txt', offset=len(expected))
        self.assertTrue(eof['eof']); self.assertEqual(eof['text'], '')
        index = self.read(reply['run'], 'manifest.json')
        self.assertEqual(index['sha256'], reply['manifest_sha256'])


if __name__ == '__main__': unittest.main()
