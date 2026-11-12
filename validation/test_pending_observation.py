"""Real local driver observations and continuation markers, without device work."""
import copy
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'validation'))
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
from pipeline import Pipeline, main, sha
import test_evaluate_pipeline as evaluation_fixture


def supervised_original(folder, intent):
    """Construct exact synthetic original bytes; no process or device is started."""
    spec = importlib.util.spec_from_file_location('test_original_supervisor', ROOT / 'examples/scatter/npu_job_supervisor.py')
    supervisor = importlib.util.module_from_spec(spec); spec.loader.exec_module(supervisor)
    birth = {'platform': 'darwin', 'start_seconds': '1794401504', 'start_microseconds': '504640'}
    module_sha = sha(Path(supervisor.__file__))
    request = {'schema_version': supervisor.REQUEST_SCHEMA,
        'intent': {key: intent[key] for key in ('action_id', 'attempt', 'source_sha256')} | {'mode': 'max', 'device': 0},
        'remote_root': str(Path(intent['remote_attempt']).parent), 'canonical_root': str(Path(intent['remote_attempt']).parent), 'remote_attempt': intent['remote_attempt'],
        'supervisor_sha256': module_sha, 'argv': ['python3', '-B', '/remote/tools/npu_benchmark.py']}
    request_raw = (json.dumps(request, indent=2) + '\n').encode()
    handle = {'schema_version': supervisor.HANDLE_SCHEMA, 'pid': 123, 'argv': request['argv'],
        'started_ns': '1794401504504640402', 'attempt': intent['remote_attempt'], 'process_birth': birth,
        'supervisor': {'pid': 122, 'birth': dict(birth, start_microseconds='400000'), 'code_sha256': module_sha},
        'execution_request_sha256': hashlib.sha256(request_raw).hexdigest(),
        **{key: request[key] for key in ('remote_root', 'canonical_root', 'remote_attempt')}}
    launch_raw = (json.dumps(handle, indent=2) + '\n').encode()
    supervisor.validate_launch_receipt(launch_raw, request_raw, intent=intent)
    for name, raw in [('observed-launch.json', launch_raw), ('observed-execution-request.json', request_raw)]:
        (folder / name).write_bytes(raw)
    observation = {'handle': handle, 'identity_verified': True, 'alive': False, 'result': None, 'result_sha256': None,
        'original_launch_sha256': hashlib.sha256(launch_raw).hexdigest(),
        'original_launch_bytes': base64.b64encode(launch_raw).decode(),
        'execution_request_sha256': hashlib.sha256(request_raw).hexdigest(),
        'execution_request_bytes': base64.b64encode(request_raw).decode()}
    return request, handle, observation


def original_failed_exit(folder, intent, handle, observation):
    """Synthetic original supervisor bytes, accepted only by the real validator."""
    receipt = {'schema_version': 'kernelbridge.native-execution-exit/v1',
        'intent': {key: intent[key] for key in ('action_id', 'attempt', 'source_sha256')} | {'mode': 'max', 'device': 0},
        'handle': handle, 'supervisor': handle['supervisor'], 'process_birth': handle['process_birth'],
        **{key: handle[key] for key in ('remote_root', 'canonical_root', 'remote_attempt', 'execution_request_sha256')},
        'returncode': -9, 'signal': 9, 'finished_ns': '1794401505504640402'}
    raw = (json.dumps(receipt, indent=2) + '\n').encode(); hashed = hashlib.sha256(raw).hexdigest()
    (folder / 'execution-exit.json').write_bytes(raw)
    observed = {**observation, 'execution_exit': receipt, 'execution_exit_sha256': hashed,
                'execution_exit_bytes': base64.b64encode(raw).decode()}
    (folder / 'observation.json').write_text(json.dumps(observed))
    failure = {'status': 'failed', 'correct': False, 'nonqualifying': True, 'result_origin': 'supervisor_exit',
        'failure_stage': 'process_exit', 'source_sha256': intent['source_sha256'], 'action_id': intent['action_id'],
        'attempt': intent['attempt'], 'remote_attempt': intent['remote_attempt'], 'remote_result_sha256': None,
        'original_handle': handle, 'execution_exit_sha256': hashed, 'execution_exit_path': str(folder / 'execution-exit.json'),
        'returncode': -9, 'signal': 9, 'error': 'Original process exited with return code -9'}
    for name in ('execution-failure.json', 'result.json'): (folder / name).write_text(json.dumps(failure))
    return raw


DRIVER = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[6])
from observation import envelope,read_documents,launch_validator
r=json.load(sys.stdin);kind=sys.argv[1];native=Path(sys.argv[2]);gate=Path(sys.argv[3]);calls=Path(sys.argv[4]);tamper=Path(sys.argv[5])
with calls.open('a') as f:f.write(json.dumps({'kind':kind,'id':r['action_id'],'source':r['source']['sha256'],'handle':r.get('handle')})+'\\n')
if not (native/'intent.json').exists():
 native.mkdir(parents=True,exist_ok=True)
 intent={'action_id':r['action_id'],'attempt':1,'source_sha256':r['source']['files']['candidate.py'],'remote_attempt':'/original/job'}
 handle={'pid':123,'argv':['native','--candidate','/original/job/candidate.py'],'started_ns':1794401504504640402,'attempt':'/original/job'}
 for name,value in [('intent.json',intent),('launch.json',{'existing':False,'handle':handle}),('observation.json',{'handle':handle,'alive':True,'result':None,'result_sha256':None})]:
  (native/name).write_text(json.dumps(value))
 (native/'candidate.py').write_bytes((Path(r['source']['root'])/'candidate.py').read_bytes())
if tamper.exists() and tamper.read_text()=='noack':(native/'launch.json').unlink(missing_ok=True)
documents,hashes,raw=read_documents(native)
if gate.exists():
 handle=documents.get('observed-launch.json') or documents['launch.json']['handle']
 value={'status':'completed','phase':'complete','job_status':'completed','handle':handle}
 if tamper.exists() and tamper.read_text()=='terminal_handle':value['handle']=dict(handle,pid=999)
 if tamper.exists() and tamper.read_text()=='native_adapter':
  sys.path.insert(0,str(Path(sys.argv[7]).parent))
  from npu_pipeline_driver import execute
  contract=json.loads(json.dumps(r['contract']));oracle=contract['oracle']['files']
  contract['oracle']['files']={name:str(Path(oracle['root'])/name) for name in oracle['files']}
  value=execute({'state':str(native.parent),'contract':contract,'mode':'max','device':0},kind,r)
else:
 original=r.get('recovery',{}).get('action_id',r['action_id'])
 validator=Path(sys.argv[7]);validator_sha=__import__('hashlib').sha256(validator.read_bytes()).hexdigest()
 if tamper.exists() and tamper.read_text()=='native_adapter':
  sys.path.insert(0,str(validator.parent))
  from npu_pipeline_driver import pending_projection
  value=pending_projection({'state':str(native.parent)},kind,r,{'status':'unknown','data':{'original_handle':documents.get('observed-launch.json')}})
 else:
  value=envelope(r,kind,original,documents,hashes,raw_files=raw,validator=launch_validator(validator,validator_sha),validator_sha256=validator_sha)
  output=Path(r['output'])/'observation';output.mkdir()
  for name,data in raw.items():(output/name).write_bytes(data)
 if tamper.exists():
  mutation=tamper.read_text()
  if mutation=='handle':value['original_handle']['pid']+=1
  elif mutation=='binding':value['binding']=dict(value['binding'],source='foreign')
  elif mutation=='path':value['artifacts']['../launch.json']=value['artifacts'].pop('launch.json')
print(json.dumps(value))
'''


class PendingObservationTests(unittest.TestCase):
    def setUp(self):
        self.h = evaluation_fixture.EvaluatePipelineTests(methodName='runTest'); self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.root = self.h.root
        self.h.gate.unlink()
        self.native = self.root / 'attempts/attempt-0001'
        self.tamper = self.root / 'tamper'
        path = self.root / 'observation-driver.py'; path.write_text(DRIVER)
        validator = ROOT / 'examples/scatter/npu_job_supervisor.py'
        for kind in ('launch', 'collect'):
            self.h.config['drivers'][kind] = {'kind': kind,
                'argv': [sys.executable, '-B', str(path), kind, str(self.native), str(self.h.gate), str(self.h.calls_path), str(self.tamper), str(ROOT / 'examples/operator_pipeline'), str(validator)],
                'files': {str(path): sha(path), str(validator): sha(validator)}, 'timeout': 5,
                'observation_validator': str(validator),
                'observed_receipt_protocol': 'kernelbridge.driver-observation/v1'}

    def first(self):
        return self.h.evaluate(wait_seconds=5)

    def resume(self, first):
        pending = self.h.result(first)
        continuation = pending['continuation']
        return self.h.call(continuation['tool'], **dict(continuation['arguments'], wait_seconds=5))

    def test_unknown_original_handle_is_visible_without_settling_and_recovers_once(self):
        first = self.first(); pending = self.h.result(first)
        state = Pipeline(self.h.config)
        self.assertEqual(pending['evaluation_status'], 'pending')
        self.assertEqual(pending['original_handle']['pid'], 123)
        self.assertEqual(pending['original_handle']['started_ns'], '1794401504504640402')
        self.assertTrue(pending['observation_only'])
        self.assertEqual(state.state['handles'], []); self.assertEqual(len(state.pending_launches()), 1)
        launch_run = state.pending_launches()[0]['run']
        marker = first['continuation_progress']
        self.assertEqual(marker['identity'], 'pipeline-launch:' + launch_run)
        self.assertEqual(marker['state'], 'progress')
        again = self.resume(first)
        self.assertEqual(again['continuation_progress']['identity'], marker['identity'])
        self.assertEqual(again['continuation_progress']['fingerprint'], marker['fingerprint'])
        self.assertEqual(len(self.h.calls('launch')), 1); self.assertEqual(self.h.calls('check'), [])
        self.h.source.write_text('replacement good bytes\n')
        blocked = self.h.result(self.h.evaluate())
        self.assertEqual(blocked['evaluation_status'], 'pending'); self.assertIsNone(blocked['original_handle'])
        self.assertNotIn('pending_observation', blocked)
        self.h.call('pipeline_run', stage='E', driver='launch', expected='failed')
        self.assertEqual(len(self.h.calls('launch')), 1)
        self.h.gate.touch()
        done = self.resume(first)
        self.assertEqual(self.h.result(done)['evaluation_status'], 'completed')
        self.assertEqual(done['continuation_progress']['identity'], marker['identity'])
        self.assertEqual(done['continuation_progress']['state'], 'progress')
        self.assertNotEqual(done['continuation_progress']['fingerprint'], marker['fingerprint'])
        state = Pipeline(self.h.config)
        self.assertEqual(len(state.state['handles']), 1); self.assertEqual(state.pending_launches(), [])
        self.assertEqual(state.state['handles'][0]['launch_run'], launch_run)
        self.assertEqual(len(self.h.calls('check')), 1)
        duplicate = self.h.call('pipeline_evaluate')
        self.assertEqual(duplicate['continuation_progress']['fingerprint'], done['continuation_progress']['fingerprint'])
        self.assertEqual(len(self.h.calls('launch')), 1); self.assertEqual(len(self.h.calls('check')), 1)

    def test_unobserved_dead_job_waits_and_wrapper_ids_do_not_create_progress(self):
        first = self.first()
        path = self.native / 'observation.json'; observed = json.loads(path.read_text()); observed['alive'] = False
        path.write_text(json.dumps(observed))
        waiting = self.resume(first); again = self.resume(waiting)
        self.assertEqual(waiting['continuation_progress']['state'], 'waiting')
        self.assertEqual(waiting['continuation_progress'], {**again['continuation_progress'], 'receipt_refs': waiting['continuation_progress']['receipt_refs']})
        self.assertEqual(Pipeline(self.h.config).state['handles'], [])
        self.assertEqual(len(Pipeline(self.h.config).pending_launches()), 1)

    def test_original_terminal_keeps_launch_identity_after_later_source_is_frozen(self):
        first = self.first(); launch_run = Pipeline(self.h.config).pending_launches()[0]['run']
        self.h.source.write_text('new independently frozen candidate\n')
        self.h.call('pipeline_freeze', files={'candidate.py': str(self.h.source)}, origin={'kind': 'later source'})
        self.h.gate.touch()
        recovered = self.h.call('pipeline_run', stage='E', driver='collect', recover_run=launch_run)
        self.assertEqual(recovered['continuation_progress']['identity'], first['continuation_progress']['identity'])
        self.assertEqual(recovered['continuation_progress']['state'], 'progress')
        self.assertEqual(Pipeline(self.h.config).pending_launches(), [])
        self.assertEqual(len(self.h.calls('launch')), 1)

    def test_unopted_driver_cannot_publish_observed_handle(self):
        for kind in ('launch', 'collect'): self.h.config['drivers'][kind].pop('observed_receipt_protocol')
        reply = self.first(); pending = self.h.result(reply)
        self.assertIsNone(pending['original_handle'])
        state = Pipeline(self.h.config)
        self.assertEqual(state.state.get('observations', []), [])
        self.assertEqual(state.state['handles'], []); self.assertEqual(len(state.pending_launches()), 1)

    def test_lost_ack_can_recover_verified_original_bytes_without_settling_the_launch(self):
        # Before original byte recovery, a later observer's PID is not authority.
        self.tamper.write_text('noack'); first = self.first()
        self.assertIsNone(self.h.result(first)['original_handle'])
        self.assertEqual(first['continuation_progress']['state'], 'waiting')
        intent = json.loads((self.native / 'intent.json').read_bytes())
        _, handle, observation = supervised_original(self.native, intent)
        (self.native / 'observation.json').write_text(json.dumps(observation))
        self.tamper.unlink()
        resumed = self.resume(first)
        self.assertEqual(self.h.result(resumed)['original_handle'], handle)
        self.assertEqual(first['continuation_progress']['identity'], resumed['continuation_progress']['identity'])
        self.assertEqual(resumed['continuation_progress']['state'], 'waiting')
        state = Pipeline(self.h.config)
        self.assertEqual(state.state['handles'], []); self.assertEqual(len(state.pending_launches()), 1)
        self.assertEqual(len(self.h.calls('launch')), 1)

    def test_provisional_result_survives_native_adapter_and_pipeline_without_terminal_authority(self):
        self.tamper.write_text('noack'); first = self.first()
        intent = json.loads((self.native / 'intent.json').read_bytes())
        _, handle, observed = supervised_original(self.native, intent)
        result = {'status': 'completed', 'correct': True, 'source_sha256': intent['source_sha256'],
                  'cases': [{'id': 'all', 'speedup': 100}], 'geomean_speedup': 100}
        observed.update(result=result, result_sha256=hashlib.sha256(json.dumps(result).encode()).hexdigest(),
                        lifecycle_state='unknown', alive=False)
        (self.native / 'observation.json').write_text(json.dumps(observed)); self.tamper.write_text('native_adapter')
        reply = self.resume(first); pending = self.h.result(reply)
        self.assertEqual(pending['original_handle'], handle)
        self.assertTrue(pending['observation_only']); self.assertEqual(pending['evaluation_status'], 'pending')
        state = Pipeline(self.h.config)
        self.assertEqual(state.state['handles'], []); self.assertEqual(len(state.pending_launches()), 1)
        observation = pending['pending_observation']['observation_state']
        self.assertEqual(observation['provisional_result']['authority'], 'observation_only')
        self.assertFalse(observation['provisional_result']['bytes_hash_verified_locally'])
        self.assertNotIn('cases', observation); self.assertNotIn('speedup', observation)
        self.assertEqual(reply['continuation_progress']['state'], 'waiting')
        self.assertEqual(self.h.calls('check'), [])
        self.assertFalse((self.native / 'device-result.json').exists())


    def test_verified_settling_window_preserves_fence_then_settles_only_original_exit(self):
        self.tamper.write_text('noack'); first = self.first()
        intent = json.loads((self.native / 'intent.json').read_bytes())
        _, handle, observed = supervised_original(self.native, intent)
        observed.update(lifecycle_state='settling', alive=False, supervisor_alive=True)
        (self.native / 'observation.json').write_text(json.dumps(observed)); self.tamper.write_text('native_adapter')
        settling = self.resume(first); result = self.h.result(settling)
        projection = result['pending_observation']['observation_state']
        self.assertEqual(projection['status'], 'settling')
        self.assertTrue(projection['supervisor_alive']); self.assertTrue(projection['supervisor_identity_verified'])
        self.assertEqual(settling['continuation_progress']['state'], 'progress')
        self.assertEqual(settling['continuation_progress']['identity'], first['continuation_progress']['identity'])
        ReadyBoundaryTests.assert_consumer_can_continue(self, settling, previous=first)
        observed['observer_timestamp_ns'] = '9999999999999999999'
        (self.native / 'observation.json').write_text(json.dumps(observed))
        again = self.resume(settling)
        self.assertEqual(again['continuation_progress']['fingerprint'], settling['continuation_progress']['fingerprint'])
        state = Pipeline(self.h.config); launch_run = state.pending_launches()[0]['run']
        self.assertEqual(state.state['handles'], []); self.assertEqual(len(state.pending_launches()), 1)
        self.assertEqual(len(self.h.calls('launch')), 1); self.assertEqual(self.h.calls('check'), [])
        raw = original_failed_exit(self.native, intent, handle, observed); self.h.gate.touch()
        terminal = self.h.call('pipeline_run', stage='E', driver='collect', recover_run=launch_run, identity='settling-terminal')
        self.assertEqual(terminal['continuation_progress']['identity'], settling['continuation_progress']['identity'])
        self.assertEqual(terminal['continuation_progress']['state'], 'progress')
        self.assertEqual(terminal['result']['result']['job_status'], 'failed')
        state = Pipeline(self.h.config)
        self.assertEqual(state.pending_launches(), []); self.assertEqual(len(state.state['handles']), 1)
        self.assertTrue(state.state['handles'][0]['collected'])
        self.assertEqual(state.state['handles'][0]['handle'], handle)
        self.assertEqual((self.native / 'execution-exit.json').read_bytes(), raw)
        self.assertFalse((self.native / 'device-result.json').exists())
        calls = list(self.h.calls('collect'))
        with self.assertRaises(FileExistsError):
            self.h.call('pipeline_run', stage='E', driver='collect', recover_run=launch_run, identity='settling-terminal')
        self.assertEqual(self.h.calls('collect'), calls); self.assertEqual(len(self.h.calls('launch')), 1)

    def test_settling_requires_original_supervisor_proof_and_liveness(self):
        self.tamper.write_text('noack'); first = self.first()
        intent = json.loads((self.native / 'intent.json').read_bytes())
        _, handle, base = supervised_original(self.native, intent)
        base.update(lifecycle_state='settling', alive=False, supervisor_alive=True)
        originals = {name: (self.native / name).read_bytes() for name in
                     ('observed-launch.json', 'observed-execution-request.json')}
        self.tamper.write_text('native_adapter')
        for mutation in ('dead', 'lost', 'identity', 'birth', 'request_proof'):
            with self.subTest(mutation=mutation):
                for name, raw in originals.items(): (self.native / name).write_bytes(raw)
                observed = copy.deepcopy(base)
                if mutation == 'dead': observed['supervisor_alive'] = False
                elif mutation == 'lost': observed.pop('supervisor_alive')
                elif mutation == 'identity': observed['identity_verified'] = False
                elif mutation == 'birth': observed['handle']['supervisor']['birth']['start_seconds'] = '0'
                else: (self.native / 'observed-execution-request.json').unlink()
                (self.native / 'observation.json').write_text(json.dumps(observed))
                reply = self.resume(first)
                self.assertEqual(reply['continuation_progress']['state'], 'waiting')
                state = Pipeline(self.h.config)
                self.assertEqual(state.state['handles'], []); self.assertEqual(len(state.pending_launches()), 1)
        self.assertEqual(len(self.h.calls('launch')), 1); self.assertEqual(self.h.calls('check'), [])

    def test_legacy_dead_child_cannot_claim_settling_with_a_liveness_boolean(self):
        first = self.first()
        path = self.native / 'observation.json'; observed = json.loads(path.read_bytes())
        observed.update(alive=False, lifecycle_state='settling', supervisor_alive=True, identity_verified=True)
        path.write_text(json.dumps(observed))
        reply = self.resume(first)
        projection = self.h.result(reply)['pending_observation']['observation_state']
        self.assertNotEqual(projection['status'], 'settling')
        self.assertNotIn('supervisor_identity_verified', projection)
        self.assertEqual(reply['continuation_progress']['state'], 'waiting')
        self.assertEqual(Pipeline(self.h.config).state['handles'], [])

    def test_forged_terminal_handle_cannot_clear_observed_original_launch_fence(self):
        first = self.first(); self.h.gate.touch(); self.tamper.write_text('terminal_handle')
        failed = self.resume(first)
        self.assertEqual(self.h.result(failed)['evaluation_status'], 'pending')
        state = Pipeline(self.h.config)
        self.assertEqual(state.state['handles'], []); self.assertEqual(len(state.pending_launches()), 1)
        self.assertEqual(self.h.result(failed)['original_handle']['pid'], 123)
        self.tamper.unlink(); done = self.resume(first)
        self.assertEqual(self.h.result(done)['evaluation_status'], 'completed')
        self.assertEqual(len(self.h.calls('launch')), 1)

    def test_forged_terminal_handle_cannot_replace_an_existing_retained_handle(self):
        self.h.gate.touch(); self.first(); self.tamper.write_text('terminal_handle')
        state = Pipeline(self.h.config); original = copy.deepcopy(state.state['handles'])
        reply = self.h.call('pipeline_run', stage='E', driver='collect', handle=original[0]['handle'], expected='failed')
        self.assertIn('verified original launch', self.h.result(reply)['error'])
        self.assertEqual(Pipeline(self.h.config).state['handles'], original)

    def test_forged_handle_binding_and_artifact_paths_fail_closed(self):
        for mutation in ('handle', 'binding', 'path'):
            with self.subTest(mutation=mutation):
                self.tamper.write_text(mutation)
                if not self.native.exists():
                    first = self.first()
                else:
                    first = self.resume(first)
                self.assertIsNone(self.h.result(first)['original_handle'])
                self.assertEqual(Pipeline(self.h.config).state.get('observations', []), [])
                self.assertEqual(len(self.h.calls('launch')), 1)

    def test_committed_observation_bytes_are_rechecked(self):
        self.first(); state = Pipeline(self.h.config)
        record = state.state['observations'][-1]
        path = state.root / record['run'] / 'driver/observation/launch.json'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            Pipeline(self.h.config).pending_launches()

    def test_artifact_page_progress_is_stable_and_keeps_exact_byte_offsets(self):
        self.h.gate.touch(); done = self.h.evaluate()
        run = done['run']
        first = self.h.read(run, 'result.json', offset=0, limit=16)
        again = self.h.read(run, 'result.json', offset=0, limit=16)
        following = self.h.read(run, 'result.json', offset=first['next_offset'], limit=16)
        self.assertEqual(first['continuation_progress'], again['continuation_progress'])
        self.assertEqual(first['continuation_progress']['identity'], following['continuation_progress']['identity'])
        self.assertNotEqual(first['continuation_progress']['fingerprint'], following['continuation_progress']['fingerprint'])
        self.assertEqual(first['continuation_progress']['receipt_refs'][0]['arguments']['offset'], 0)
        self.assertEqual(following['continuation_progress']['receipt_refs'][0]['arguments']['offset'], first['next_offset'])


class NativePendingProjectionTests(unittest.TestCase):
    def setUp(self):
        import test_npu_pipeline_driver as fixture
        self.h = fixture.NpuPipelineDriverTest(methodName='runTest'); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.adapter = fixture.adapter
        self.intent = json.loads((self.h.folder / 'intent.json').read_text())
        self.intent['remote_attempt'] = self.h.handle['attempt']
        (self.h.folder / 'intent.json').write_text(json.dumps(self.intent))
        (self.h.folder / 'launch.json').write_text(json.dumps({'existing': False, 'handle': self.h.handle}))
        self.observation = {'result': None, 'result_sha256': None, 'alive': True, 'handle': self.h.handle}
        (self.h.folder / 'observation.json').write_text(json.dumps(self.observation))
        for name in ('result.json', 'device-result.json'): (self.h.folder / name).unlink()
        output = self.h.root / 'pending-projection'; output.mkdir()
        self.request = dict(self.h.request, action_id='recover-wrapper', role='E', output=str(output),
                            recovery={'action_id': self.intent['action_id']}, handle=None)

    def test_adapter_preserves_original_bytes_and_action_bound_handle(self):
        with patch.object(self.adapter, 'trial', return_value={'status': 'unknown', 'data': {
                'record': str(self.h.folder / 'observation.json'), 'original_handle': self.h.handle}}) as collect:
            result = self.adapter.execute(self.h.config, 'collect', self.request)
        self.assertEqual(result['status'], 'unknown'); self.assertEqual(result['original_handle'], self.adapter.wire_handle(self.h.handle))
        self.assertEqual(result['launch_action_id'], self.intent['action_id'])
        self.assertEqual(collect.call_args.kwargs, {'recover': 1})
        for name in result['artifacts']:
            self.assertEqual((Path(self.request['output']) / 'observation' / name).read_bytes(), (self.h.folder / name).read_bytes())
        self.assertFalse((self.h.folder / 'result.json').exists())

    def test_collect_cannot_select_another_action_with_the_same_source(self):
        request = dict(self.request, recovery={'action_id': 'foreign-launch'})
        with patch.object(self.adapter, 'trial', side_effect=AssertionError('No foreign recovery')) as collect:
            with self.assertRaisesRegex(ValueError, 'No unique original'):
                self.adapter.execute(self.h.config, 'collect', request)
        collect.assert_not_called()

    def test_foreign_outcome_hints_and_ack_mismatches_fail_closed(self):
        for hint in ({'source_sha256': 'foreign'}, {'record': '/older/observation.json'},
                     {'original_handle': dict(self.h.handle, pid=99)}):
            with self.subTest(hint=hint), patch.object(self.adapter, 'trial', return_value={'status': 'unknown', 'data': hint}):
                with self.assertRaises(ValueError): self.adapter.execute(self.h.config, 'collect', self.request)
        (self.h.folder / 'launch.json').write_text(json.dumps({'handle': dict(self.h.handle, pid=99)}))
        with patch.object(self.adapter, 'trial', return_value={'status': 'unknown', 'data': {}}):
            with self.assertRaisesRegex(ValueError, 'acknowledgement'):
                self.adapter.execute(self.h.config, 'collect', self.request)

    def test_lost_ack_requires_exact_original_launch_and_request_bytes(self):
        _, handle, observation = supervised_original(self.h.folder, self.intent)
        (self.h.folder / 'launch.json').unlink()
        (self.h.folder / 'observation.json').write_text(json.dumps(observation))
        before = {p.name: p.read_bytes() for p in self.h.folder.iterdir()}
        with patch.object(self.adapter, 'trial', return_value={'status': 'unknown', 'data': {'original_handle': handle}}):
            result = self.adapter.execute(self.h.config, 'collect', self.request)
        self.assertEqual(result['original_handle'], handle)
        self.assertEqual(result['observation_state']['status'], 'missing_result')
        self.assertEqual({p.name: p.read_bytes() for p in self.h.folder.iterdir()}, before)
        self.assertFalse((self.h.folder / 'launch.json').exists())
        from observation import envelope, read_documents
        documents, hashes, raw = read_documents(self.h.folder)
        with self.assertRaisesRegex(ValueError, 'pinned validator'):
            envelope(self.request, 'collect', self.intent['action_id'], documents, hashes, raw_files=raw)
        for name in ('observed-launch.json', 'observed-execution-request.json'):
            path = self.h.folder / name; original = path.read_bytes(); path.write_bytes(original + b' ')
            with self.subTest(name=name), patch.object(self.adapter, 'trial', return_value={'status': 'unknown', 'data': {}}):
                with self.assertRaises(ValueError): self.adapter.execute(self.h.config, 'collect', self.request)
            path.write_bytes(original)


class NativeFailureProjectionTests(unittest.TestCase):
    def setUp(self):
        import test_npu_pipeline_driver as fixture
        import npu_job_supervisor as supervisor
        self.h = fixture.NpuPipelineDriverTest(methodName='runTest'); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.adapter = fixture.adapter
        self.intent = json.loads((self.h.folder / 'intent.json').read_text()) | {'remote_attempt': '/remote/original'}
        (self.h.folder / 'intent.json').write_text(json.dumps(self.intent))
        execution_request, self.handle, self.observation = supervised_original(self.h.folder, self.intent)
        self.receipt = {'schema_version': supervisor.EXIT_SCHEMA, 'intent': execution_request['intent'],
            'handle': self.handle, 'supervisor': self.handle['supervisor'], 'process_birth': self.handle['process_birth'],
            **{key: self.handle[key] for key in ('remote_root', 'canonical_root', 'remote_attempt', 'execution_request_sha256')},
            'returncode': -9, 'signal': 9, 'finished_ns': '1794401505504640402'}
        self.raw = (json.dumps(self.receipt, indent=2) + '\n').encode(); self.exit_sha = hashlib.sha256(self.raw).hexdigest()
        supervisor.validate_exit_receipt(self.raw, handle=self.handle, intent=self.intent)
        (self.h.folder / 'execution-exit.json').write_bytes(self.raw)
        self.observation.update(execution_exit=self.receipt, execution_exit_sha256=self.exit_sha,
            execution_exit_bytes=base64.b64encode(self.raw).decode())
        (self.h.folder / 'observation.json').write_text(json.dumps(self.observation))
        self.failure = {'status': 'failed', 'correct': False, 'nonqualifying': True, 'result_origin': 'supervisor_exit',
            'failure_stage': 'process_exit', 'source_sha256': self.intent['source_sha256'], 'action_id': self.intent['action_id'],
            'attempt': 1, 'remote_attempt': self.intent['remote_attempt'], 'remote_result_sha256': None,
            'original_handle': self.handle, 'execution_exit_sha256': self.exit_sha,
            'execution_exit_path': str(self.h.folder / 'execution-exit.json'), 'returncode': -9, 'signal': 9,
            'error': 'Original process exited with return code -9'}
        for name in ('execution-failure.json', 'result.json'): (self.h.folder / name).write_text(json.dumps(self.failure))
        (self.h.folder / 'device-result.json').unlink()
        self.config = dict(self.h.config, mode='max', device=0)
        self.request = dict(self.h.request, action_id='recover-wrapper', handle=self.handle,
            recovery={'action_id': self.intent['action_id']})

    def test_original_exit_is_readable_but_cannot_qualify_F_G_or_diagnostics(self):
        before = {p.name: p.read_bytes() for p in self.h.folder.iterdir()}
        with patch.object(self.adapter, 'trial', side_effect=AssertionError('No retry')), \
                patch.object(self.adapter, 'transport', side_effect=AssertionError('No device access')):
            result = self.adapter.execute(self.config, 'collect', self.request)
            self.assertEqual(result['status'], 'completed'); self.assertEqual(result['job_status'], 'failed')
            self.assertTrue(result['nonqualifying']); self.assertEqual(result['scope'], 'supervisor_exit')
            self.assertEqual(result['evaluation']['execution_exit_sha256'], self.exit_sha)
            self.assertEqual((Path(self.request['output']) / 'evaluation/original-execution-exit.json').read_bytes(), self.raw)
            checked = self.adapter.execute(self.config, 'check', self.request)
            self.assertEqual(checked['checks'], dict.fromkeys(('build', 'semantics', 'full'), False))
            self.assertEqual(checked['case_count'], 0)
            with self.assertRaisesRegex(ValueError, 'no device measurements'):
                self.adapter.execute(self.config, 'measure', self.request)
            for kind in ('ir', 'profiler'):
                self.assertEqual(self.adapter.execute(self.config, kind, self.request)['status'], 'unavailable')
        self.assertEqual({p.name: p.read_bytes() for p in self.h.folder.iterdir()}, before)
        self.assertFalse((self.h.folder / 'device-result.json').exists())

    def test_changed_original_exit_bytes_identity_and_false_measurements_are_rejected(self):
        for mutation in ('bytes', 'source', 'action', 'birth', 'handle', 'summary'):
            with self.subTest(mutation=mutation):
                receipt = copy.deepcopy(self.receipt); failure = copy.deepcopy(self.failure)
                if mutation == 'source': receipt['intent']['source_sha256'] = '0' * 64
                elif mutation == 'action': receipt['intent']['action_id'] = 'foreign'
                elif mutation == 'birth': receipt['process_birth']['start_seconds'] = '0'
                elif mutation == 'handle': receipt['handle']['pid'] += 1
                elif mutation == 'summary': failure['cases'] = [{'correct': True, 'speedup': 2}]
                raw = self.raw + b' ' if mutation == 'bytes' else (json.dumps(receipt, indent=2) + '\n').encode()
                (self.h.folder / 'execution-exit.json').write_bytes(raw)
                (self.h.folder / 'result.json').write_text(json.dumps(failure))
                with self.assertRaises(ValueError): self.adapter.execute(self.config, 'collect', self.request)
        (self.h.folder / 'execution-exit.json').write_bytes(self.raw)

    def test_null_ack_uses_verified_original_bytes_but_wrong_nonnull_ack_is_rejected(self):
        path = self.h.folder / 'launch.json'; path.write_text(json.dumps({'existing': False, 'handle': None}))
        originals = {name: (self.h.folder / name).read_bytes() for name in
            ('observation.json', 'execution-exit.json', 'execution-failure.json', 'result.json')}
        early = copy.deepcopy(self.observation)
        for key in ('execution_exit', 'execution_exit_sha256', 'execution_exit_bytes'): early.pop(key)
        (self.h.folder / 'observation.json').write_text(json.dumps(early))
        for name in ('execution-exit.json', 'execution-failure.json', 'result.json'): (self.h.folder / name).unlink()
        with patch.object(self.adapter, 'trial', return_value={'status': 'unknown', 'data': {'original_handle': self.handle}}):
            pending = self.adapter.execute(self.config, 'collect', self.request)
        self.assertEqual(pending['status'], 'unknown'); self.assertEqual(pending['original_handle'], self.handle)
        self.assertFalse((self.h.folder / 'result.json').exists())
        for name, raw in originals.items(): (self.h.folder / name).write_bytes(raw)
        result = self.adapter.execute(self.config, 'collect', self.request)
        self.assertEqual(result['job_status'], 'failed'); self.assertEqual(result['handle'], self.handle)
        path.write_text(json.dumps({'existing': False, 'handle': dict(self.handle, pid=999)}))
        with self.assertRaisesRegex(ValueError, 'original launch acknowledgement'):
            self.adapter.execute(self.config, 'collect', self.request)


class ReadyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.h = evaluation_fixture.EvaluatePipelineTests(methodName='runTest'); self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def assert_consumer_can_continue(self, reply, previous=None):
        for path in ROOT.glob('source/packages/*/src'): sys.path.insert(0, str(path))
        from kernelcat_executor.state.continuation_progress import continuation_activity
        events = [
            {'seq': 2, 'kind': 'admitted', 'action': {'id': 'evaluate', 'tool': 'pipeline_evaluate', 'task': 'task', 'arguments': {}},
             'application_context': {'input_id': 'input', 'goal_id': 'goal'}},
            {'seq': 3, 'kind': 'settled', 'action_id': 'evaluate', 'outcome': {'status': 'completed', 'data': reply}},
        ]
        if previous is not None:
            events = [
                {'seq': 2, 'kind': 'admitted', 'action': {'id': 'original-wait', 'tool': 'pipeline_evaluate', 'task': 'task', 'arguments': {}},
                 'application_context': {'input_id': 'input', 'goal_id': 'goal'}},
                {'seq': 3, 'kind': 'settled', 'action_id': 'original-wait', 'outcome': {'status': 'completed', 'data': previous}},
                *[{**event, 'seq': event['seq'] + 2} for event in events],
            ]
        actual = continuation_activity(events, {'input_id': 'input', 'accepted_seq': 1, 'completed_seq': 6 if previous is not None else 4},
            task_id='task', goal_id='goal', start_seq=0)
        self.assertFalse(actual['waiting']); self.assertTrue(actual['progress']); self.assertFalse(actual['invalid_signal'])

    def test_zero_wait_before_first_launch_leaves_executable_work_not_external_waiting(self):
        reply = self.h.evaluate(wait_seconds=0); result = self.h.result(reply)
        self.assertEqual(result['pending_kind'], 'phase_ready')
        self.assertEqual(reply['continuation_progress']['state'], 'progress')
        self.assert_consumer_can_continue(reply)
        self.assertEqual(self.h.calls('launch'), [])
        continuation = result['continuation']
        done = self.h.call(continuation['tool'], **dict(continuation['arguments'], wait_seconds=2))
        self.assertEqual(self.h.result(done)['evaluation_status'], 'completed')
        self.assertEqual(len(self.h.calls('launch')), 1); self.assertEqual(len(self.h.calls('check')), 1)

    def test_original_terminal_job_at_deadline_can_continue_to_F_without_new_launch(self):
        self.h.call('pipeline_freeze', files={'candidate.py': str(self.h.source)}, origin={'kind': 'fixture'})
        launched = self.h.call('pipeline_run', stage='E', driver='launch')
        reply = self.h.evaluate(wait_seconds=0); result = self.h.result(reply)
        self.assertEqual(result['pending_kind'], 'phase_ready')
        self.assertEqual(reply['continuation_progress']['identity'], launched['continuation_progress']['identity'])
        self.assert_consumer_can_continue(reply)
        continuation = result['continuation']
        done = self.h.call(continuation['tool'], **dict(continuation['arguments'], wait_seconds=2))
        self.assertEqual(self.h.result(done)['evaluation_status'], 'completed')
        self.assertEqual(len(self.h.calls('launch')), 1); self.assertEqual(len(self.h.calls('check')), 1)


class PreparedImportClosureTests(unittest.TestCase):
    def test_fresh_process_loads_prepared_tools_and_native_preflight_with_minimal_environment(self):
        import test_npu_pipeline_driver as fixture
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); transport = root / 'transport.py'
            transport.write_text('raise AssertionError("No transport import is allowed for local preflight")\n')
            output = root / 'prepared'
            args = SimpleNamespace(output=output, task=None, profile=None, runtime_profile=None, mode='max', device=0,
                transport=transport, remote_root='/remote/task', python=Path(sys.executable))
            fixture.prepare(args)
            tool = json.loads((output / 'business-tools.json').read_text())[0]
            config = json.loads((output / 'pipeline-tools/config.json').read_text())
            self.assertTrue(any(Path(pin['path']).name == 'observation.py' for pin in tool['files']))
            for name in ('npu-launch', 'npu-collect'):
                driver = config['drivers'][name]
                self.assertEqual(driver['files'][driver['observation_validator']], sha(driver['observation_validator']))
                for required in ('npu_job_supervisor.py', 'observation.py'):
                    self.assertTrue(any(Path(path).name == required and value == sha(path) for path, value in driver['files'].items()))
            env = {'PATH': os.environ['PATH'], 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}
            status = subprocess.run(tool['argv'], input=json.dumps({'id': 'fresh-process', 'tool': 'pipeline_status', 'arguments': {}}),
                cwd=output / 'pipeline-tools', env=env, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(status.stdout)['status'], 'completed')
            validator_probe = '''import json
from observation import launch_validator
c=json.load(open('config.json')); d=c['drivers']['npu-collect']; p=d['observation_validator']
validate=launch_validator(p,d['files'][p])
try: validate(b'{}',b'{}',intent={})
except ValueError as error:
 assert str(error)=='Invalid original execution request schema', str(error)
else: raise AssertionError('Malformed original request was accepted')
'''
            subprocess.run([sys.executable, '-B', '-c', validator_probe],
                cwd=output / 'pipeline-tools', env=env, text=True, capture_output=True, check=True)
            candidate = output / 'project/candidate.py'; candidate.write_text('kernel_src = "incomplete six-source interface"\n')
            source = {'root': str(candidate.parent), 'files': {'candidate.py': sha(candidate)}}
            source['sha256'] = fixture.adapter.manifest_sha(source['files'])
            native_config = json.loads((output / 'trial-config.json').read_text())
            request = {'action_id': 'preflight-only', 'source': source, 'binding': {'source': source['sha256']},
                'contract': native_config['contract'], 'role': 'E', 'output': str(root / 'native-preflight')}
            # Contract mismatch is rejected before parsing or importing transport.
            request['contract'] = copy.deepcopy(request['contract']); request['contract']['task']['outputs'] = 'mismatch'
            paths = request['contract']['oracle']['files']
            oracle = {'root': str(Path(next(iter(paths.values()))).parent), 'files': {name: sha(path) for name, path in paths.items()}}
            oracle['sha256'] = fixture.adapter.manifest_sha(oracle['files'])
            request['contract']['oracle']['files'] = oracle
            reply = subprocess.run(config['drivers']['npu-launch']['argv'], input=json.dumps(request),
                cwd=output / 'pipeline-tools', env=env, text=True, capture_output=True)
            self.assertEqual(reply.returncode, 0, reply.stderr)
            self.assertEqual(json.loads(reply.stdout)['dispatch_outcome'], 'not_started')
            self.assertEqual(list((output / 'attempts').iterdir()), [])


if __name__ == '__main__': unittest.main()
