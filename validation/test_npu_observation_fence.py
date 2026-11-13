"""CPU-only original-evidence and bounded-query tests; no device invocation."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from npu_isolation_fixture import build_fixture, isolation, seal, snapshot


def validate(f):
    return isolation.validate_evidence(f['samples_raw'], f['receipt_raw'], f['request_raw'], f['exit_raw'],
                                      handle=f['handle'], intent=f['intent'], policy=f['policy'])


def clocks(mono, wall, duration=1):
    return dict(started_mono_ns=str(mono), ended_mono_ns=str(mono+duration),
                started_ns=str(wall), ended_ns=str(wall+duration))


def wait_record(child, mono, wall, *, nonblocking=True):
    return {'schema': 'kernelbridge.child-wait-observation/v1', 'pid': child['pid'], 'parent_pid': child['ppid'],
            'options': ['WEXITED', 'WNOWAIT'] + (['WNOHANG'] if nonblocking else []),
            'result': {'si_pid': child['pid'], 'si_uid': child['uid'], 'si_signo': 17, 'si_status': 0, 'si_code': 1},
            **clocks(mono, wall)}


def fenced_fixture():
    f = build_fixture(); child = f['receipt']['own_root']; row = f['samples'][2]
    wall = int(f['handle']['started_ns']) + 1980000000; mono = 11980000000
    row.update(observed_ns=str(wall), ended_ns=str(wall+10000000),
               started_mono_ns=str(mono), ended_mono_ns=str(mono+10000000))
    row['root_before_inventory'] = {'identity': copy.deepcopy(child), **clocks(mono+10, wall+10)}
    row['inventory'].update(clocks(mono+20, wall+20, 1000000))
    stat = f"{child['pid']} (original child) " + ' '.join(['Z', str(child['ppid'])] + ['0']*17 + [child['birth']['start_ticks']] + ['0']*8)
    proof = {'schema': 'kernelbridge.unreaped-child/v1', 'stat_before': stat, 'stat_after': stat,
             'status_after': f"Pid:\t{child['pid']}\nPPid:\t{child['ppid']}\nNSpid:\t{child['pid']}\nUid:\t{child['uid']}\t{child['uid']}\t{child['uid']}\t{child['uid']}\n",
             'boot_id': child['birth']['boot_id'], 'namespaces_after': {'pid': child['pid_ns'], 'mnt': None},
             'wait': wait_record(child, mono+3000000, wall+3000000), **clocks(mono+2000000, wall+2000000, 2000000)}
    row['processes'][0].update(identity=copy.deepcopy(child), lineage=[copy.deepcopy(child)],
                               lineage_source='unreaped_direct_child', exit_fence=proof)
    f['handle']['exit_protocol'] = 'linux_waitid_wnowait/v1'
    f['exit'].update(exit_authority='linux_waitid_wnowait',
        wait_observation=wait_record(child, mono+3000000, wall+3000000, nonblocking=False))
    seal(f)
    final = f['samples'][-1]; reap_mono = int(final['ended_mono_ns']) + 10; reap_wall = int(final['ended_ns']) + 10
    f['receipt']['child_reap'] = {'pid': child['pid'], 'birth': child['birth'], 'returncode': 0,
        'execution_exit_sha256': isolation.digest(f['exit_raw']),
        'wait_before': wait_record(child, reap_mono+1, reap_wall+1), **clocks(reap_mono, reap_wall, 3)}
    return seal(f)


class ObservationFenceTests(unittest.TestCase):
    def test_guard_joins_inflight_sample_and_records_final_before_reap(self):
        f = fenced_fixture(); events = []
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve(); (folder / 'execution-request.json').write_bytes(f['request_raw'])
            guard = isolation.Guard(folder, f['request'], isolation.digest(f['request_raw']))
            guard.handle = f['handle']; guard.own_root = f['receipt']['own_root']
            guard.thread = SimpleNamespace(join=lambda: events.append('joined'))
            def final(phase):
                self.assertTrue(guard.stop.is_set()); self.assertEqual(events, ['joined'])
                self.assertEqual(phase, 'final'); events.append('final')
                guard.stream.write(f['samples_raw']); guard.stream.flush()
            guard.sample = final
            def reap():
                self.assertEqual(events, ['joined', 'final', 'wait-before']); events.append('reaped'); return 0
            def wait_before(*args, **kwargs):
                self.assertEqual(events, ['joined', 'final']); events.append('wait-before')
                return f['receipt']['child_reap']['wait_before']
            try:
                with patch.object(isolation.processes, 'wait_child_exit', side_effect=wait_before), \
                     patch.object(isolation.processes, 'validate_child_wait', return_value=0), \
                     patch.object(isolation, 'validate_evidence', return_value={'verdict': 'unknown'}):
                    guard.finish(f['exit_raw'], reap=reap)
                receipt = json.loads((folder / 'isolation-receipt.json').read_bytes())
                self.assertEqual(events, ['joined', 'final', 'wait-before', 'reaped'])
                self.assertEqual(receipt['child_reap']['returncode'], 0)
                self.assertEqual(receipt['child_reap']['execution_exit_sha256'], isolation.digest(f['exit_raw']))
            finally: guard.close()

    def test_pure_exact_child_exit_and_final_reap_prove_sampled_clear(self):
        f = fenced_fixture()
        with patch.object(isolation.processes, 'identify_process', side_effect=AssertionError('live read')), \
             patch.object(isolation.subprocess, 'run', side_effect=AssertionError('device call')):
            result = validate(f)
        self.assertEqual(result['verdict'], isolation.CLAIM)
        self.assertEqual(result['result_sha256'], f['result_sha256'])

    def test_missing_live_pre_snapshot_and_changed_child_identity_stay_unknown(self):
        for mode in ('missing', 'dead', 'birth', 'namespace', 'parent', 'observer'):
            with self.subTest(mode=mode):
                f = fenced_fixture(); row = f['samples'][2]; pre = row['root_before_inventory']['identity']
                if mode == 'missing': del row['root_before_inventory']
                elif mode == 'dead': pre['state'] = 'Z'
                elif mode == 'birth': pre['birth']['start_ticks'] = '999'
                elif mode == 'namespace': pre['mnt_ns'] = 'mnt:[999]'
                elif mode == 'parent': f['receipt']['own_root'] = {**f['receipt']['own_root'], 'ppid': 999}
                else: row['observer'] = {**row['observer'], 'pid': 999}
                self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_raw_kernel_and_wait_fields_cannot_borrow_known_exit(self):
        for mode in ('stat', 'uid', 'nspid', 'wait_uid', 'wait_code', 'clock', 'foreign'):
            with self.subTest(mode=mode):
                f = fenced_fixture(); row = f['samples'][2]; item = row['processes'][0]; proof = item['exit_fence']
                if mode == 'stat': proof['stat_after'] = proof['stat_after'].replace(' 200 ', ' 999 ')
                elif mode == 'uid': proof['status_after'] = proof['status_after'].replace('1000', '9999')
                elif mode == 'nspid': proof['status_after'] = proof['status_after'].replace('NSpid:\t200', 'NSpid:\t201')
                elif mode == 'wait_uid': proof['wait']['result']['si_uid'] = 999
                elif mode == 'wait_code': proof['wait']['result'].update(si_code=2, si_status=15)
                elif mode == 'clock': proof['started_ns'] = '1'
                else: item['reported']['reported_pid'] = 999
                self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_final_stale_child_cannot_reuse_an_earlier_live_snapshot(self):
        f = fenced_fixture(); final = f['samples'][-1]; final.update(snapshot([(0, 200)]))
        final['processes'] = copy.deepcopy(f['samples'][2]['processes'])
        self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_final_reap_is_required_and_must_match_raw_wait(self):
        for mode in ('missing', 'returncode', 'uid', 'raw', 'order', 'rounded'):
            with self.subTest(mode=mode):
                f = fenced_fixture(); reap = f['receipt']['child_reap']
                if mode == 'missing': del f['receipt']['child_reap']
                elif mode == 'returncode': reap['returncode'] = 7
                elif mode == 'uid': reap['wait_before']['result']['si_uid'] = 999
                elif mode == 'raw': del reap['wait_before']
                elif mode == 'order': reap['started_mono_ns'] = '1'
                else: reap['ended_ns'] = int(reap['ended_ns'])
                self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_original_timeout_reason_has_exact_raw_navigation(self):
        f = build_fixture(); row = f['samples'][2]
        row['error'] = 'TimeoutExpired: original bounded query'
        row['inventory']['error'] = {'type': 'TimeoutExpired', 'message': 'original bounded query'}
        row['ended_mono_ns'] = str(int(row['started_mono_ns']) + 2015556042)
        result = validate(seal(f)); reasons = result['structured_reasons']
        self.assertEqual(result['verdict'], 'unknown')
        self.assertEqual(reasons[0]['code'], 'observation_command_error')
        self.assertEqual(reasons[0]['command'], 'inventory')
        self.assertEqual(reasons[0]['line'], 3)
        lines = f['samples_raw'].splitlines(keepends=True)
        self.assertEqual(reasons[0]['byte_offset'], sum(map(len, lines[:2])))
        self.assertEqual(reasons[0]['sha256'], hashlib.sha256(f['samples_raw']).hexdigest())
        self.assertEqual(reasons[0]['line_sha256'], hashlib.sha256(lines[2]).hexdigest())
        self.assertIn('observation_duration_exceeded', [v['code'] for v in reasons])
        self.assertEqual(result['result_sha256'], f['result_sha256'])

    def test_old_disappeared_process_is_unknown_with_original_row_reference(self):
        f = build_fixture(); item = f['samples'][2]['processes'][0]
        item.clear(); item.update(reported={'board': 0, 'chip': 0, 'reported_pid': 200},
                                   error='Reported process disappeared before identity capture')
        result = validate(seal(f))
        self.assertEqual(result['verdict'], 'unknown')
        self.assertEqual(result['structured_reasons'][0]['code'], 'process_identity_unavailable')
        self.assertEqual(result['structured_reasons'][0]['reported_pid'], 200)

    def test_failed_query_missing_inventory_and_domain_keeps_original_error(self):
        f = build_fixture(); row = f['samples'][2]
        row['error'] = 'OSError: original inventory unavailable'
        for key in ('inventory', 'domain', 'target', 'driver'): del row[key]
        result = validate(seal(f))
        self.assertEqual(result['verdict'], 'unknown')
        self.assertEqual(result['reason'], row['error'])
        self.assertEqual(result['structured_reasons'][0]['line'], 3)
        self.assertEqual(result['result_sha256'], f['result_sha256'])

    def test_reason_list_is_bounded_without_changing_unknown(self):
        f = build_fixture(); row = f['samples'][2]
        row['processes'] = [{'error': 'Original PID identity unavailable', 'reported': {'reported_pid': i}} for i in range(40)]
        result = validate(seal(f))
        self.assertEqual(result['verdict'], 'unknown')
        self.assertEqual(len(result['structured_reasons']), 16)
        self.assertGreaterEqual(result['omitted_reasons'], 24)

    def test_trial_projection_preserves_original_reason_navigation(self):
        import npu_trial
        f = build_fixture(); f['samples'][2]['error'] = 'Original query timed out'; seal(f)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            for name, raw in f['raw'].items(): (folder / name).write_bytes(raw)
            (folder / 'observation.json').write_text(json.dumps({'result_sha256': f['result_sha256']}))
            config = {'mode': 'fixture', 'device': 0, 'remote_root': f['request']['remote_root'],
                      'contract': {'measurement': {'device_isolation': {'policy': f['policy'],
                          'policy_sha256': isolation.policy_digest(f['policy'])}}}}
            intent = {**f['intent'], 'remote_attempt': f['request']['remote_attempt']}
            projected = npu_trial.isolation_projection(config, folder, intent, f['handle'])
        self.assertEqual(projected['verdict'], 'unknown')
        self.assertEqual(projected['structured_reasons'], validate(f)['structured_reasons'])
        self.assertEqual(projected['structured_reasons'][0]['sha256'], projected['artifacts']['isolation-samples.jsonl'])

    def test_mapping_and_inventory_share_deadline_and_retain_independent_windows(self):
        f = build_fixture(); barrier = threading.Barrier(2); observed_timeouts = []
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'npu-smi'; binary.write_bytes(b'CPU fixture, never executed')
            def query(argv, *, capture_output, timeout):
                observed_timeouts.append(timeout); barrier.wait(timeout=1)
                key = 'mapping' if argv[-1] == '-m' else 'inventory'
                return subprocess.CompletedProcess(argv, 0, snapshot()[key]['stdout'].encode(), b'')
            with patch.object(isolation.processes, 'identify_process', return_value=f['samples'][0]['observer']), \
                 patch.object(isolation.shutil, 'which', return_value=str(binary)), \
                 patch.object(isolation, '_validate_view'), patch.object(isolation.subprocess, 'run', side_effect=query):
                sample = isolation.observe_target(f['policy'])
        self.assertNotIn('error', sample)
        self.assertEqual(len(observed_timeouts), 2)
        self.assertTrue(all(0 < value <= 2 for value in observed_timeouts))
        for name in ('mapping', 'inventory'):
            self.assertEqual(sample[name]['returncode'], 0)
            self.assertLessEqual(int(sample[name]['started_mono_ns']), int(sample[name]['ended_mono_ns']))

    def test_timeout_preserves_partial_command_bytes_and_other_response(self):
        f = build_fixture(); barrier = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'npu-smi'; binary.write_bytes(b'CPU fixture')
            def query(argv, *, capture_output, timeout):
                barrier.wait(timeout=1)
                if argv[-1] != '-m': raise subprocess.TimeoutExpired(argv, timeout, output=b'partial inventory', stderr=b'original error')
                return subprocess.CompletedProcess(argv, 0, snapshot()['mapping']['stdout'].encode(), b'')
            with patch.object(isolation.processes, 'identify_process', return_value=f['samples'][0]['observer']), \
                 patch.object(isolation.shutil, 'which', return_value=str(binary)), \
                 patch.object(isolation.subprocess, 'run', side_effect=query):
                sample = isolation.observe_target(f['policy'])
        self.assertIn('TimeoutExpired', sample['error'])
        self.assertEqual(sample['mapping']['returncode'], 0)
        self.assertEqual(sample['inventory']['stdout'], 'partial inventory')
        self.assertEqual(sample['inventory']['stderr'], 'original error')
        self.assertIsNone(sample['inventory']['returncode'])


if __name__ == '__main__': unittest.main()
