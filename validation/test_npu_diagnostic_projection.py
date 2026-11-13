"""Original NPU diagnostic projections with local receipts and a no-network transport.

The transport executes only the adapter's real bounded READ_ARTIFACT script on
temporary files. No compiler, model, NPU launch or measurement is performed.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import test_npu_pipeline_driver as fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
from pipeline import Pipeline, main
from validation.npu_isolation_fixture import preparation_profile

adapter = fixture.adapter


class OriginalNpuDiagnosticFixture(unittest.TestCase):
    def setUp(self):
        fixture.NpuPipelineDriverTest.setUp(self)
        self.job = self.root / 'remote-original/job'; self.job.mkdir(parents=True)
        self.handle = dict(self.handle, attempt=str(self.job.parent))
        self.request['handle'] = adapter.wire_handle(self.handle)
        self.calls = self.root / 'transport-calls.jsonl'
        self.transport = self.root / 'read-only-transport.py'
        self.transport.write_text('''import hashlib,json,subprocess,sys
from pathlib import Path
def run(argv, *, stdin, timeout):
    assert argv[:3] == ['python3', '-B', '-c']
    assert hashlib.sha256(argv[3].encode()).hexdigest() == SCRIPT_SHA, 'No dispatch or substitute script is allowed'
    request=json.loads(stdin); assert timeout == 45
    assert request['limit'] <= 262144 and request['offset'] >= 0
    assert Path(argv[4]).resolve() == Path(JOB).resolve(), 'Only the original job is readable'
    with Path(CALLS).open('a') as stream: stream.write(json.dumps({'operation': 'read_original_artifact', 'job': argv[4], 'path': argv[5], 'offset': request['offset'], 'limit': request['limit']})+'\\n')
    result = subprocess.run([sys.executable, *argv[1:]], input=stdin, capture_output=True, timeout=timeout)
    assert len(result.stdout) < 360000, 'Artifact page exceeded bounded transport frame'
    return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
''' + '\nSCRIPT_SHA = ' + repr(hashlib.sha256(adapter.READ_ARTIFACT.encode()).hexdigest())
            + '\nJOB = ' + repr(str(self.job)) + '\nCALLS = ' + repr(str(self.calls)) + '\n')
        self.config['transport_module'] = str(self.transport)
        self.result['diagnostics'] = {'reports': []}
        self.seal_result()


    def seal_result(self):
        original = json.dumps(self.result, indent=2, allow_nan=False) + '\n'
        digest = hashlib.sha256(original.encode()).hexdigest()
        summary = {'status': self.result['status'], 'correct': self.result['correct'],
                   'remote_result_sha256': digest, 'original_handle': self.handle,
                   'remote_attempt': str(self.job.parent)}
        observation = {'result': self.result, 'result_sha256': digest, 'cpu_fallback': False, 'log_tail': ''}
        for name, value in [('device-result.json', self.result), ('result.json', summary), ('observation.json', observation)]:
            (self.folder / name).write_text(json.dumps(value))
        (self.job / 'result.json').write_text(original)
        (self.job.parent / 'launch.json').write_text(json.dumps(self.handle))
        (self.job.parent / 'intent.json').write_bytes((self.folder / 'intent.json').read_bytes())
        (self.job.parent / 'candidate.py').write_bytes((self.folder / 'candidate.py').read_bytes())


    def artifact(self, kind, data=None):
        suffix = '.ll' if kind == 'ir' else '.csv'
        path = self.job / (kind + suffix)
        path.write_bytes(data if data is not None else (b'define void @fixture() { ret void }\n' if kind == 'ir' else b'name,duration\nfixture,1\n'))
        return {'kind': kind, 'status': 'completed', 'scope': 'npu_diagnostic',
                'candidate_sha256': self.source['files']['candidate.py'],
                'artifacts': {path.name: adapter.sha(path)}}


    def project(self, kind, **overrides):
        output = self.root / ('projection-' + uuid.uuid4().hex); output.mkdir()
        request = dict(self.request, output=str(output), role='J', **overrides)
        return adapter.execute(self.config, kind, request), output


    def reads(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []


    def prepared_policy(self, switches=None):
        profile = self.root / ('synthetic-pid-view-' + uuid.uuid4().hex + '.json')
        values = preparation_profile()
        if switches is not None:
            values['diagnostics'] = switches
        profile.write_text(json.dumps(values))
        output = self.root / ('prepared-' + uuid.uuid4().hex)
        fixture.prepare(SimpleNamespace(output=output, task=None, profile=profile, runtime_profile=None,
            mode='max', device=0, transport=self.transport, remote_root='/never/dispatched', python=Path(sys.executable)))
        configuration = json.loads((output / 'pipeline-tools/config.json').read_text())
        # This only changes projections. Native collection, suite and timing are unchanged.
        trial = json.loads((output / 'trial-config.json').read_text())
        self.assertEqual(trial['profile_attempts'], [1])
        self.assertEqual((trial['pairs'], trial['warmup'], trial['repeats']), (3, 3, 10))
        self.assertEqual(len(trial['contract']['suite']), 6)
        self.assertFalse((output / 'project/candidate.py').exists())
        return configuration['diagnostics']


    def pipeline(self, switches=None, *, frozen=True, retained=True):
        config_path = self.root / 'original-adapter.json'; config_path.write_text(json.dumps(self.config))
        policy = self.prepared_policy(switches)
        config = {'state': str(self.root / 'workflow'), 'read_roots': [str(self.root)], 'diagnostics': policy,
            'drivers': {name: {'kind': kind, 'argv': [sys.executable, '-B', str(ROOT / 'examples/scatter/npu_pipeline_driver.py'), str(config_path), operation],
                'files': {str(path): adapter.sha(path) for path in (config_path, self.transport, ROOT / 'examples/scatter/npu_pipeline_driver.py')}}
                for name, kind, operation in [('npu-check', 'check', 'check'), ('npu-measure', 'measure', 'measure'),
                                             ('npu-ir', 'diagnostic', 'ir'), ('npu-profiler', 'diagnostic', 'profiler')]}}
        pipeline = Pipeline(config)
        self.apply(pipeline, 'pipeline_bind', contract=self.config['contract'])
        self.apply(pipeline, 'pipeline_plan', stage='B', record={'family': 'local original receipt fixture'})
        self.apply(pipeline, 'pipeline_plan', stage='C', record={'hypothesis': 'fixture source, not a performance claim'})
        if frozen:
            self.apply(pipeline, 'pipeline_freeze', files={'candidate.py': str(self.folder / 'candidate.py')}, origin={'scope': 'protocol_fixture'})
        self.apply(pipeline, 'pipeline_baseline', selection='files', files={'npu_eager.py': str(self.oracle / 'npu_eager.py')})
        # Install a retained, already-collected original handle as fixture state.
        # No launch driver is configured: all subsequent requests must project it.
        if retained:
            pipeline.state['handles'].append({'handle': adapter.wire_handle(self.handle), 'source': copy.deepcopy(pipeline.current()),
                'contract': copy.deepcopy(pipeline.state['contract']), 'binding': pipeline.binding(), 'collected': str(self.folder / 'result.json')})
        return pipeline


    def apply(self, pipeline, tool, **arguments):
        result = pipeline.apply({'id': uuid.uuid4().hex, 'tool': tool, 'arguments': arguments})
        self.assertEqual(result['status'], 'completed', result)
        # Production loads a fresh Pipeline per tool call. Refresh its journal
        # parent here while retaining the test's original-handle fixture state.
        pipeline.chain = Pipeline(pipeline.config).chain
        return result['data']



class NpuDiagnosticProjectionTests(OriginalNpuDiagnosticFixture):
    def test_J_downloads_original_bytes_and_preserves_both_source_identities(self):
        self.result['diagnostics']['reports'] = [self.artifact('ir'), self.artifact('profiler')]
        self.seal_result()
        for kind in ('ir', 'profiler'):
            result, output = self.project(kind)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['candidate_sha256'], self.source['sha256'])  # pipeline manifest
            self.assertEqual(result['native_candidate_sha256'], self.source['files']['candidate.py'])
            self.assertEqual(result['original_handle'], adapter.wire_handle(self.handle))
            self.assertTrue(result['read_only_projection'])
            self.assertEqual(result['original_job'], str(self.job))
            self.assertEqual(len(result['artifacts']), 1)
            for name, expected in result['artifacts'].items():
                self.assertEqual(adapter.sha(output / name), expected)
                self.assertIn(expected, result['original_artifacts'].values())
        self.assertEqual([row['operation'] for row in self.reads()], ['read_original_artifact'] * 2)


    def test_unavailable_retains_attempt_log_and_never_becomes_completed_IR(self):
        log = self.job / 'compiler.log'; log.write_text('unsupported option: -emit-llvm\n')
        self.result['diagnostics']['reports'] = [{'kind': 'ir', 'status': 'unavailable',
            'scope': 'npu_diagnostic', 'candidate_sha256': self.source['files']['candidate.py'],
            'reason': 'Compiler does not support LLVM emission', 'attempt_evidence': {log.name: adapter.sha(log)}}]
        self.seal_result()
        result, output = self.project('ir')
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('does not support', result['reason'])
        self.assertEqual((output / 'artifacts/ir-0.log').read_text(), log.read_text())
        absent, _ = self.project('profiler')
        self.assertEqual(absent['status'], 'unavailable')
        self.assertEqual(len(self.reads()), 1)


    def test_changed_missing_empty_and_oversize_original_artifacts_are_rejected(self):
        for boundary in ('changed', 'missing', 'empty', 'oversize'):
            with self.subTest(boundary=boundary):
                record = self.artifact('ir', b'original')
                path = self.job / 'ir.ll'
                if boundary == 'changed': path.write_bytes(b'tampered')
                elif boundary == 'missing': path.unlink()
                else:
                    with path.open('wb') as stream: stream.truncate(0 if boundary == 'empty' else adapter.MAX_ARTIFACT + 1)
                    record['artifacts'][path.name] = adapter.sha(path)
                self.result['diagnostics']['reports'] = [record]; self.seal_result()
                if boundary == 'oversize':
                    result, output = self.project('ir')
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertFalse(result['collection_complete']); self.assertEqual(result['artifacts'], {})
                    self.assertEqual(result['original_artifacts'], record['artifacts'])
                    failure = result['unavailable_artifacts'][0]
                    self.assertEqual(failure['bytes'], adapter.MAX_ARTIFACT + 1)
                    self.assertEqual(failure['expected_sha256'], record['artifacts']['ir.ll'])
                    self.assertIn('collection bound', failure['reason'])
                    self.assertEqual(list((output / 'artifacts').iterdir()), [])
                else:
                    with self.assertRaisesRegex(RuntimeError, 'hash changed|missing'):
                        self.project('ir')
        self.assertEqual(len(self.reads()), 4)


    def test_larger_than_eight_MiB_is_reconstructed_in_bounded_pages(self):
        size = 9 * 1024 * 1024 + 17
        record = self.artifact('ir', b'x' * size)
        self.result['diagnostics']['reports'] = [record]; self.seal_result()
        result, output = self.project('ir')
        path = output / next(iter(result['artifacts']))
        self.assertEqual(path.stat().st_size, size)
        self.assertEqual(adapter.sha(path), record['artifacts']['ir.ll'])
        self.assertEqual([row['offset'] for row in self.reads()], list(range(0, size, adapter.LOG_PAGE_BYTES)))
        self.assertTrue(result['collection_complete'])


    def test_page_change_and_symlink_are_rejected_without_partial_artifact(self):
        for kind in ('changed-page', 'symlink'):
            record = self.artifact('ir', b'x' * (adapter.LOG_PAGE_BYTES + 17))
            self.result['diagnostics']['reports'] = [record]; self.seal_result()
            path = self.job / 'ir.ll'
            real_transport = adapter.transport(self.config)
            def read(argv, **kwargs):
                reply = real_transport(argv, **kwargs)
                if json.loads(kwargs['stdin'])['offset'] == 0:
                    path.write_bytes(b'y' * (adapter.LOG_PAGE_BYTES + 17))
                return reply
            if kind == 'symlink':
                target = self.root / 'outside.ll'; target.write_bytes(path.read_bytes())
                path.unlink(); path.symlink_to(target)
            with self.subTest(kind=kind), patch.object(adapter, 'transport', return_value=read if kind == 'changed-page' else real_transport), \
                    self.assertRaisesRegex(RuntimeError, 'changed|linked'):
                self.project('ir')
            self.assertEqual(list(self.root.glob('projection-*/artifacts/*.ll')), [])
            self.assertEqual(list(self.root.glob('projection-*/artifacts/*.tmp')), [])
            if path.is_symlink(): path.unlink()


    def test_original_result_source_and_handle_are_checked_before_download(self):
        self.result['diagnostics']['reports'] = [self.artifact('ir')]; self.seal_result()
        with self.assertRaisesRegex(ValueError, 'handle differs'):
            self.project('ir', handle={'pid': 99})
        original = copy.deepcopy(self.result)
        self.result['source_sha256'] = 'a' * 64; self.seal_result()
        with self.assertRaisesRegex(ValueError, 'candidate identity changed'):
            self.project('ir')
        self.result = original; self.seal_result()
        (self.folder / 'candidate.py').write_text('changed frozen candidate')
        with self.assertRaisesRegex(ValueError, 'Frozen source changed'):
            self.project('ir')
        self.assertEqual(self.reads(), [])


    def test_remote_original_identity_is_checked_before_reading_a_page(self):
        for kind in ('source', 'handle', 'result', 'intent'):
            self.result['diagnostics']['reports'] = [self.artifact('ir')]; self.seal_result()
            if kind == 'source': (self.job.parent / 'candidate.py').write_bytes(b'other source')
            elif kind == 'handle':
                (self.job.parent / 'launch.json').write_text(json.dumps(dict(self.handle, pid=99)))
            elif kind == 'result': (self.job / 'result.json').write_text('{}\n')
            else: (self.job.parent / 'intent.json').write_text('{}\n')
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, 'changed'):
                self.project('ir')
        self.assertEqual(list(self.root.glob('projection-*/artifacts/*.ll')), [])


    def test_prepare_defaults_and_explicit_boolean_overrides(self):
        default = self.prepared_policy()
        self.assertEqual({key: default[key] for key in ('on_failure', 'on_regression', 'required')},
                         {'on_failure': True, 'on_regression': True, 'required': False})
        for switches in ({'on_failure': False, 'on_regression': False}, {'required': True}):
            with self.subTest(switches=switches):
                policy = self.prepared_policy(switches)
                for key, value in switches.items(): self.assertEqual(policy[key], value)
                self.assertEqual(policy['drivers'], {'ir': 'npu-ir', 'profiler': 'npu-profiler'})
                self.assertEqual(policy['scope'], 'npu_diagnostic')
        self.assertEqual(self.reads(), [])


    def test_prepare_rejects_invalid_switches_and_driver_scope_overrides(self):
        for switches in (False, [], {'on_failure': 1}, {'required': 'true'}, {'scope': 'local_cpu'}, {'drivers': {}}):
            with self.subTest(switches=switches), self.assertRaisesRegex(ValueError, 'diagnostic'):
                self.prepared_policy(switches)
        self.assertEqual(self.reads(), [])


    def test_F_failure_projects_original_IR_and_unavailable_profiler_then_advises_C(self):
        self.result.update(status='failed', correct=False, cases=[], message='original correctness failure')
        self.result['diagnostics']['reports'] = [self.artifact('ir'), {'kind': 'profiler', 'status': 'unavailable',
            'reason': 'Do not execute an incorrect candidate', 'scope': 'npu_diagnostic'}]
        self.seal_result(); pipeline = self.pipeline()
        reply = self.apply(pipeline, 'pipeline_run', stage='F', driver='npu-check')
        reports = reply['diagnostics']['reports']
        self.assertEqual([row['status'] for row in reports], ['completed', 'unavailable'])
        self.assertEqual(reply['workflow']['next_actions'][0]['stage'], 'C')
        self.assertEqual(reports[0]['trigger']['handle'], adapter.wire_handle(self.handle))
        self.assertEqual(reports[0]['source'], pipeline.current()['sha256'])
        read = main(pipeline.config, {'id': uuid.uuid4().hex, 'tool': 'pipeline_read',
            'arguments': {'run': reply['run'], 'path': 'diagnostic-ir/driver/artifacts/ir-0.ll'}})
        self.assertEqual(read['status'], 'completed')
        self.assertIn('@fixture', read['data']['text'])
        self.assertEqual(len(self.reads()), 1)


    def test_G_regression_downloads_once_and_reuses_original_diagnostics(self):
        self.result['diagnostics']['reports'] = [self.artifact('ir'), self.artifact('profiler')]
        self.seal_result(); pipeline = self.pipeline()
        self.apply(pipeline, 'pipeline_run', stage='F', driver='npu-check')
        self.assertEqual(self.reads(), [])  # a passing F does not always profile
        paired = self.apply(pipeline, 'pipeline_pair', driver='npu-measure')
        diagnostics = paired['diagnostics']
        self.assertEqual(diagnostics['trigger']['reason'], 'regression')
        self.assertEqual([row['status'] for row in diagnostics['reports']], ['completed', 'completed'])
        self.assertEqual(paired['workflow']['next_actions'][0]['stage'], 'C')
        repeated = self.apply(pipeline, 'pipeline_pair', driver='npu-measure')
        self.assertTrue(all(row['reused'] for row in repeated['diagnostics']['reports']))
        self.assertEqual(len(self.reads()), 2)


    def test_explicit_projection_opt_out_makes_no_artifact_requests(self):
        self.result['diagnostics']['reports'] = [self.artifact('ir'), self.artifact('profiler')]
        self.seal_result(); pipeline = self.pipeline({'on_failure': False, 'on_regression': False})
        self.apply(pipeline, 'pipeline_run', stage='F', driver='npu-check')
        paired = self.apply(pipeline, 'pipeline_pair', driver='npu-measure')
        self.assertNotIn('diagnostics', paired)
        self.assertEqual(self.reads(), [])



if __name__ == '__main__': unittest.main()
