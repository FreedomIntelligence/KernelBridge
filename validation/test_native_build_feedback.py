"""Source-bound build feedback through the real paged read-only artifact script.

All files/processes are local protocol fixtures. No compiler, model or device is
invoked, and the transport rejects any script except READ_ARTIFACT.
"""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

import test_npu_diagnostic_projection as fixture
import test_terminal_job_feedback as terminal

adapter = fixture.adapter
ROOT = Path(__file__).resolve().parents[1]


class NativeBuildFeedbackTests(unittest.TestCase):
    seal_result = fixture.OriginalNpuDiagnosticFixture.seal_result
    reads = fixture.OriginalNpuDiagnosticFixture.reads
    call = terminal.TerminalJobFeedbackTests.call
    read_all = terminal.TerminalJobFeedbackTests.read_all

    def setUp(self):
        fixture.OriginalNpuDiagnosticFixture.setUp(self)
        self.actions = {}
        self.result.update(status='failed', correct=False, cases=[], build_completed=True,
                           build_source_digest='b' * 64, failure_stage='correctness',
                           message='The Inner error is reported as above.',
                           traceback='RuntimeError: generic asynchronous wrapper error\n')
        self.build_log = 'build/ToyMatmul/.kernelbridge-build-stage.log'
        self.metadata = 'build/ToyMatmul/.kernelbridge-build-stage.receipt.json'
        self.log_bytes = (b'build command metadata\n'
                          b'[ERROR] SDK ToyMatmul do not registe tiling struct!!!\n'
                          + ('remaining build output 中文\n' * 16000).encode())
        (self.job / self.build_log).parent.mkdir(parents=True)
        (self.job / self.build_log).write_bytes(self.log_bytes)
        (self.job / self.metadata).write_text('{"returncode":0,"fixture":true}\n')
        descriptors = {name: {'path': name, 'sha256': adapter.sha(self.job / name),
                             'bytes': (self.job / name).stat().st_size}
                       for name in (self.build_log, self.metadata)}
        self.result['build_diagnostics'] = {
            'schema_version': 'kernelbridge.native-build-diagnostics/v1', 'status': 'completed',
            'candidate_sha256': self.result['source_sha256'], 'source_digest': 'b' * 64,
            'target': self.result['soc'],
            'steps': [{'returncode': 0, 'argv': ['./build.sh'], 'log': descriptors[self.build_log],
                       'metadata': descriptors[self.metadata]}],
            'artifacts': {name: item['sha256'] for name, item in descriptors.items()}}
        self.seal_result()

    def feedback(self):
        request = dict(self.request, action_id='feedback-' + uuid.uuid4().hex,
                       output=str(self.root / ('feedback-' + uuid.uuid4().hex)))
        with patch.object(adapter, 'trial', side_effect=AssertionError('Never dispatch or compile')):
            result = adapter.execute(self.config, 'collect', request)
        return result['evaluation'], Path(request['output'])

    def retain_runtime_log(self):
        first = '[E1112 16:58:20.474 compiler_depend.ts:350] call aclnnToyMatmulGetWorkspaceSize failed: Execution_Error(EZ1008): Failed to execute tiling function.'
        data = (first + '\n' + json.dumps({'status': 'failed', 'source_sha256': self.result['source_sha256'],
            'message': '[ERROR] generic ERR00100 wrapper', 'noise': ['eager'] * 3000}) + '\n').encode()
        observation_path = self.folder / 'observation.json'
        observation = json.loads(observation_path.read_text())
        metadata = {'path': 'run.log', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                    'identity': [1, 2, 3, len(data)], 'stable': True, 'complete': True}
        observation.update(log=metadata, log_tail=data[-12000:].decode())
        observation_path.write_text(json.dumps(observation))
        (self.folder / 'run.log').write_bytes(data)
        (self.folder / 'run-log.json').write_text(json.dumps({
            'status': 'completed', 'complete': True, 'path': 'run.log', 'bytes': len(data),
            'sha256': metadata['sha256'], 'source_sha256': self.result['source_sha256'],
            'remote_result_sha256': observation['result_sha256'], 'original_handle': self.handle,
            'remote_attempt': str(self.job.parent), 'observed': metadata}))
        return first

    def pipeline(self):
        config_path = self.root / 'adapter.json'; config_path.write_text(json.dumps(self.config))
        driver = ROOT / 'examples/scatter/npu_pipeline_driver.py'
        configuration = {'state': str(self.root / 'pipeline'), 'read_roots': [str(self.root)],
            'drivers': {'original-collect': {'kind': 'launch',
                'argv': [sys.executable, '-B', str(driver), str(config_path), 'collect'],
                'files': {str(path): adapter.sha(path) for path in (driver, config_path, self.transport)}}}}
        pipeline = terminal.Pipeline(configuration)
        self.call(pipeline, 'pipeline_bind', contract=self.config['contract'])
        self.call(pipeline, 'pipeline_plan', stage='B', record={'scope': 'retained build fixture'})
        self.call(pipeline, 'pipeline_plan', stage='C', record={'hypothesis': 'read original diagnostics'})
        self.call(pipeline, 'pipeline_freeze', files={'candidate.py': str(self.folder / 'candidate.py')},
                  origin={'scope': 'offline protocol fixture'})
        return pipeline

    def test_rc_zero_sdk_error_and_complete_log_are_readable_through_pipeline(self):
        first = self.retain_runtime_log()
        pipeline = self.pipeline()
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-collect')
        evaluation = reply['result']['result']['evaluation']
        build = evaluation['build_diagnostics']
        self.assertTrue(evaluation['build_completed'])
        self.assertEqual(build['steps'][0]['returncode'], 0)
        self.assertEqual(build['status'], 'completed')
        self.assertTrue(build['collection_complete'])
        self.assertEqual(evaluation['first_sdk_diagnostic']['line'], 2)
        self.assertIn('ToyMatmul do not registe', evaluation['error_excerpt'])
        self.assertEqual(evaluation['first_runtime_error']['text'], first)
        self.assertEqual(evaluation['first_runtime_error']['line'], 1)
        artifact = build['artifacts'][self.build_log]
        self.assertEqual(self.read_all(pipeline, artifact), self.log_bytes)
        self.assertEqual(build['steps'][0]['log']['artifact'], artifact)
        self.assertGreater(sum(row['path'] == self.build_log for row in self.reads()), 1)

    def test_bound_root_alias_is_allowed_for_all_operator_modes(self):
        canonical = self.job
        alias = self.root / 'declared-work'; alias.symlink_to(self.job.parent, target_is_directory=True)
        self.job = alias / 'job'
        self.handle = dict(self.handle, attempt=str(alias)); self.request['handle'] = adapter.wire_handle(self.handle)
        self.seal_result()
        for mode in ('max', 'min', 'matmul'):
            with self.subTest(mode=mode):
                self.config['mode'] = mode
                evaluation, output = self.feedback()
                artifact = evaluation['build_diagnostics']['artifacts'][self.build_log]
                self.assertEqual((output / artifact['path']).read_bytes(), self.log_bytes)
                self.assertEqual(evaluation['build_diagnostics']['steps'][0]['returncode'], 0)
        self.assertEqual(self.job.resolve(), canonical)
        self.assertTrue(all(row['job'] == str(self.job) for row in self.reads()))

    def test_first_sdk_diagnostic_follows_exact_step_time_not_random_log_name(self):
        build = self.result['build_diagnostics']
        build['steps'][0]['started_at_ns'] = 1794401504504640402
        build['steps'][0]['ended_at_ns'] = 1794401504504640403
        earlier = 'build/ZFirst/.kernelbridge-build-z.log'
        path = self.job / earlier; path.parent.mkdir()
        path.write_bytes(b'[ERROR] EARLIEST native SDK diagnostic\n')
        item = {'path': earlier, 'sha256': adapter.sha(path), 'bytes': path.stat().st_size}
        build['artifacts'][earlier] = item['sha256']
        # Deliberately append it out of order; timestamps differ beyond JS precision.
        build['steps'].append({'returncode': 0, 'argv': ['generate'], 'log': item,
                               'started_at_ns': '1794401504504640401'})
        self.seal_result()
        evaluation, _ = self.feedback()
        self.assertIn('EARLIEST', evaluation['first_sdk_diagnostic']['text'])
        self.assertEqual(evaluation['build_diagnostics']['steps'][0]['log']['path'], earlier)
        later = evaluation['build_diagnostics']['steps'][1]
        self.assertEqual(later['started_at_ns'], '1794401504504640402')
        self.assertEqual(later['ended_at_ns'], '1794401504504640403')
        wire = subprocess.check_output(['node', '-e',
            'process.stdout.write(JSON.stringify(JSON.parse(require("node:fs").readFileSync(0,"utf8"))))'],
            input=json.dumps(evaluation).encode())
        self.assertEqual(json.loads(wire), evaluation)

    def test_internal_artifact_symlink_is_rejected_even_inside_original_job(self):
        path = self.job / self.build_log
        saved = path.read_bytes(); path.unlink()
        other = self.job / 'other.log'; other.write_bytes(saved); path.symlink_to(other)
        evaluation, _ = self.feedback()
        build = evaluation['build_diagnostics']
        self.assertEqual(build['status'], 'partial')
        self.assertNotIn(self.build_log, build['artifacts'])
        self.assertIn('linked', str(build['unavailable_artifacts']))
        self.assertTrue(evaluation['build_completed'])

    def test_internal_directory_symlink_is_rejected(self):
        folder = (self.job / self.build_log).parent
        actual = folder.with_name('actual'); folder.rename(actual); folder.symlink_to(actual, target_is_directory=True)
        evaluation, _ = self.feedback()
        self.assertEqual(evaluation['build_diagnostics']['status'], 'unavailable')
        self.assertEqual(evaluation['build_diagnostics']['artifacts'], {})

    def test_remote_source_handle_result_or_log_change_never_becomes_evidence(self):
        paths = [self.job.parent / 'candidate.py', self.job.parent / 'launch.json',
                 self.job / 'result.json', self.job / self.build_log]
        for path in paths:
            with self.subTest(path=path.name):
                before = path.read_bytes()
                try:
                    path.write_bytes(before + b' changed')
                    evaluation, _ = self.feedback()
                    self.assertNotIn(self.build_log, evaluation['build_diagnostics']['artifacts'])
                    self.assertIn(evaluation['build_diagnostics']['status'], ('partial', 'unavailable'))
                    self.assertTrue(evaluation['build_completed'])
                    self.assertEqual(evaluation['status'], 'failed')
                finally: path.write_bytes(before)

    def test_unbound_build_manifest_is_rejected_before_transport(self):
        original = copy.deepcopy(self.result['build_diagnostics'])
        for field, changed in (('candidate_sha256', 'c' * 64), ('source_digest', 'c' * 64),
                               ('target', 'different-soc'), ('artifacts', {'../outside.log': 'a' * 64})):
            with self.subTest(field=field):
                self.result['build_diagnostics'] = {**original, field: changed}; self.seal_result()
                before = len(self.reads())
                with self.assertRaises(ValueError): self.feedback()
                self.assertEqual(len(self.reads()), before)

    def test_nonzero_returncode_is_preserved_without_promoting_build(self):
        self.result['build_completed'] = False
        self.result['build_diagnostics']['steps'][0]['returncode'] = 2
        self.seal_result()
        evaluation, _ = self.feedback()
        self.assertFalse(evaluation['build_completed'])
        self.assertEqual(evaluation['build_diagnostics']['steps'][0]['returncode'], 2)
        self.assertEqual(evaluation['build_diagnostics']['status'], 'completed')

    def test_legacy_result_has_no_remote_read_and_result_json_is_not_a_diagnostic(self):
        del self.result['build_diagnostics']; self.seal_result()
        first = self.retain_runtime_log()
        evaluation, _ = self.feedback()
        self.assertEqual(self.reads(), [])
        self.assertEqual(evaluation['first_sdk_diagnostic']['text'], first)
        self.assertEqual(evaluation['first_runtime_error']['line'], 1)
        self.assertNotIn('ERR00100', evaluation['error_excerpt'])


if __name__ == '__main__': unittest.main()
