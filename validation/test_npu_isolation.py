"""CPU-only verification of raw isolation evidence; no device invocation."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from npu_isolation_fixture import build_admission_fixture, build_fixture, identity, isolation, preparation_profile, seal, snapshot


def validate(f):
    return isolation.validate_evidence(f['samples_raw'], f['receipt_raw'], f['request_raw'], f['exit_raw'],
                                      handle=f['handle'], intent=f['intent'], policy=f['policy'])


def admission(f):
    return isolation.validate_admission(f['admission_raw'], f['samples_raw'], f['request_raw'],
                                       intent=f['intent'], policy=f['policy'])


def rebind_admission(f):
    seal(f)
    _, binding = isolation._bindings(f['request_raw'], f['intent'], f['policy'])
    f['admission'].update(binding, samples_sha256=isolation.digest(f['samples_raw']))
    f['admission_raw'] = isolation.canonical(f['admission']) + b'\n'
    return f


class IsolationEvidenceTests(unittest.TestCase):
    def test_retained_policy_validation_is_pure_for_both_physical_targets(self):
        for device in (0, 1):
            with self.subTest(device=device):
                f = build_fixture(logical_device=device)
                with patch.object(isolation.processes, 'identify_process', side_effect=AssertionError('live process read')), \
                     patch.object(isolation.processes, 'resolve_observed_pid', side_effect=AssertionError('live PID resolution')), \
                     patch.object(isolation.subprocess, 'run', side_effect=AssertionError('device invocation')), \
                     patch.object(Path, 'read_bytes', side_effect=AssertionError('filesystem evidence read')):
                    normalized = isolation.validate_policy_view(f['policy'])
                self.assertEqual(normalized, f['policy'])
                self.assertIsNot(normalized, f['policy'])
                self.assertEqual(validate(f)['verdict'], isolation.CLAIM)
                profile = preparation_profile(device)
                self.assertEqual(profile['validation_fixture'], {'kind': 'synthetic_pid_view', 'device_evidence': False})
                self.assertEqual(profile['device_isolation']['policy_sha256'], isolation.policy_digest(normalized))

    def test_retained_policy_validation_rejects_missing_or_tampered_raw_proof(self):
        for kind in ('missing_view', 'missing_snapshot', 'tampered_raw', 'wrong_declared_pid', 'wrong_domain'):
            with self.subTest(kind=kind):
                policy = build_fixture()['policy']; view = policy['view_evidence']
                if kind == 'missing_view': del policy['view_evidence']
                elif kind == 'missing_snapshot': del view['raw_snapshots']['before']
                elif kind == 'tampered_raw': view['raw_snapshots']['before']['inventory']['stdout'] += 'tampered\n'
                elif kind == 'wrong_declared_pid': view['probe']['active_pids'] = [999]
                else: policy['domain_physical_ids'] = [1]
                with self.assertRaises(ValueError): isolation.validate_policy_view(policy)

    def test_expanded_domain_cannot_borrow_another_chips_holder_view(self):
        policy = build_fixture()['policy']
        policy['domain_physical_ids'] = [0, 1]
        self.assertEqual(isolation.validate_policy_view(policy)['logical_device'], 0)
        policy['logical_device'] = 1
        with self.assertRaisesRegex(ValueError, 'another physical chip'):
            isolation.validate_policy_view(policy)

    def test_raw_fixture_proves_only_sampled_cooperative_clear(self):
        f = build_fixture()
        result = validate(f)
        self.assertEqual(result['verdict'], 'sampled_observed_clear')
        self.assertEqual(result['scope'], 'cooperative_same_uid_namespace')
        self.assertTrue(result['coverage']['bounded'])
        self.assertEqual(result['result_sha256'], f['result_sha256'])
        self.assertEqual(result['execution_exit_sha256'], isolation.digest(f['exit_raw']))

    def test_handle_nanoseconds_allow_exact_string_to_integer_wire_conversion(self):
        f = build_fixture()
        f['handle']['started_ns'] = int(f['handle']['started_ns'])
        self.assertEqual(validate(f)['verdict'], isolation.CLAIM)
        f['handle']['started_ns'] += 1
        with self.assertRaises(ValueError): validate(f)

    def test_other_physical_chip_process_does_not_contaminate_target(self):
        f = build_fixture()
        for sample in (f['samples'][0], f['samples'][1], f['samples'][3]):
            sample.update(snapshot([(1, 900)]))
        self.assertEqual(validate(seal(f))['verdict'], isolation.CLAIM)

    def test_target_foreign_process_invalidates_performance_without_erasing_report(self):
        f = build_fixture()
        sample = f['samples'][3]
        sample.update(snapshot([(0, 900)]))
        _, _, rows = isolation._parsed(sample, f['policy'])
        sample['processes'] = [{'reported': rows[0], 'identity': identity(900), 'lineage': None, 'lineage_source': 'fresh'}]
        result = validate(seal(f))
        self.assertEqual(result['verdict'], 'foreign')
        self.assertEqual(result['result_sha256'], f['result_sha256'])

    def test_missing_driver_view_never_becomes_idle_clear(self):
        f = build_fixture()
        del f['policy']['view_evidence']
        self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_driver_view_requires_original_probe_snapshots(self):
        for mutate in (
            lambda view: view.pop('raw_snapshots'),
            lambda view: view['probe'].update(active_pids=[401], reported_pid=401,
                process=identity(401), active_process=identity(401)),
            lambda view: view['driver'].update(sha256='f' * 64),
            lambda view: view['observer']['birth'].update(boot_id='changed-boot'),
        ):
            with self.subTest(mutate=mutate):
                f = build_fixture(); mutate(f['policy']['view_evidence'])
                self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_header_only_or_unclosed_inventory_is_unknown(self):
        for kind in ('header_only', 'unclosed', 'bad_health', 'missing_command', 'mapping_ambiguous'):
            with self.subTest(kind=kind):
                f = build_fixture(); sample = f['samples'][3]
                if kind == 'header_only':
                    sample['inventory']['stdout'] = sample['inventory']['stdout'].split('| No running')[0]
                elif kind == 'unclosed':
                    sample['inventory']['stdout'] = sample['inventory']['stdout'].rsplit('+================+', 1)[0]
                elif kind == 'bad_health':
                    sample['inventory']['stdout'] = sample['inventory']['stdout'].replace('| OK |', '| Warning |', 1)
                elif kind == 'missing_command': sample['mapping']['returncode'] = 1
                else: sample['mapping']['stdout'] += '0 2 0 2 Ascend910\n'
                self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_visibility_remap_invalidates_isolation_even_with_valid_raw_inventory(self):
        for key in ('ASCEND_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES'):
            f = build_fixture(); f['samples'][2]['visibility'][key] = '1'
            self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_policy_cannot_downgrade_claim_bounds_or_domain(self):
        for change in ({'required': False}, {'claim': 'exclusive'}, {'scope': 'global'},
                       {'sample_timeout_ms': 10001}, {'max_gap_ms': 100}, {'logical_device': True},
                       {'domain_physical_ids': [0, 0]}, {'unknown': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                isolation.normalize_policy({'logical_device': 0, **change})

    def test_raw_bytes_source_policy_and_exit_tampering_are_rejected(self):
        for field in ('samples_raw', 'exit_raw', 'request_raw'):
            with self.subTest(field=field):
                f = build_fixture(); f[field] += b' '
                with self.assertRaises(ValueError): validate(f)
        f = build_fixture(); f['intent'] = {**f['intent'], 'source_sha256': 'c' * 64}
        with self.assertRaises(ValueError): validate(f)
        f = build_fixture(); f['policy'] = {**f['policy'], 'logical_device': 1}
        with self.assertRaises(ValueError): validate(f)

    def test_missing_or_partial_evidence_stays_unknown(self):
        f = build_fixture()
        for field in ('receipt_raw', 'samples_raw', 'exit_raw'):
            missing = dict(f); missing[field] = b''
            self.assertEqual(validate(missing)['verdict'], 'unknown')
        f['samples_raw'] = f['samples_raw'][:-1]
        f['receipt']['samples_sha256'] = isolation.digest(f['samples_raw'])
        f['receipt_raw'] = isolation.canonical(f['receipt'])
        self.assertEqual(validate(f)['verdict'], 'unknown')

    def test_timing_requires_positive_serial_bounded_lifecycle_coverage(self):
        for kind in ('negative', 'zero', 'numeric', 'gap', 'overlap', 'old_window', 'late_final'):
            with self.subTest(kind=kind):
                f = build_fixture(); samples = f['samples']
                if kind in ('negative', 'zero', 'numeric'):
                    value = {'negative': '-1', 'zero': '0', 'numeric': 10}[kind]
                    for sample in samples:
                        for key in ('started_mono_ns', 'ended_mono_ns'): sample[key] = value
                elif kind == 'gap': samples[3]['started_mono_ns'] = str(int(samples[2]['started_mono_ns']) + 6000000000)
                elif kind == 'overlap': samples[2]['started_mono_ns'] = samples[1]['started_mono_ns']
                elif kind == 'old_window':
                    for sample in samples:
                        for key in ('observed_ns', 'ended_ns'): sample[key] = str(int(sample[key]) - 100000000000)
                else:
                    for key in ('observed_ns', 'ended_ns'): samples[-1][key] = str(int(samples[-1][key]) + 6000000000)
                self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_shared_lease_schema_cannot_be_trimmed_or_rebound(self):
        for field in ('root', 'filename', 'root_dev', 'root_ino', 'dev', 'ino'):
            f = build_fixture(); f['receipt']['leases'][0].pop(field)
            self.assertEqual(validate(seal(f))['verdict'], 'unknown')
        f = build_fixture(); f['samples'][2]['lease_evidence'] = copy.deepcopy(f['samples'][2]['lease_evidence'])
        f['samples'][2]['lease_evidence'][0]['ino'] += 1
        self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_retained_lineage_requires_prior_same_birth_proof(self):
        f = build_fixture(); child = f['receipt']['own_root']; descendant = identity(201, child['pid'])
        for sample in (f['samples'][2], f['samples'][3]):
            sample.update(snapshot([(0, descendant['pid'])]))
            _, _, rows = isolation._parsed(sample, f['policy'])
            current = copy.deepcopy(descendant)
            if sample['phase'] == 'final': current['ppid'] = 1
            sample['processes'] = [{'reported': rows[0], 'identity': current, 'lineage': [child, descendant],
                                   'lineage_source': 'retained' if sample['phase'] == 'final' else 'fresh'}]
        self.assertEqual(validate(seal(f))['verdict'], isolation.CLAIM)
        f['samples'][2].update(snapshot())
        f['samples'][2]['processes'] = []
        self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_pid_reuse_is_not_own(self):
        f = build_fixture(); item = f['samples'][2]['processes'][0]
        item['identity'] = copy.deepcopy(item['identity']); item['identity']['birth']['start_ticks'] = '999'
        self.assertEqual(validate(seal(f))['verdict'], 'unknown')

    def test_all_no_start_reasons_use_real_raw_validation(self):
        for reason in ('observation_unknown', 'foreign', 'cooperative_lease_busy'):
            with self.subTest(reason=reason):
                f = build_admission_fixture(reason)
                result = admission(f)
                self.assertEqual(result['status'], 'not_started')
                self.assertIs(result['dispatched'], False)

    def test_admission_cannot_remove_or_change_producer(self):
        for owner in ({}, {'pid': 999, 'birth': identity(999)['birth'], 'code_sha256': 'a' * 64}):
            f = build_admission_fixture(); f['admission']['supervisor'] = owner
            rebind_admission(f)
            with self.assertRaises(ValueError): admission(f)

    def test_busy_admission_requires_proven_view_and_original_scope(self):
        f = build_admission_fixture('cooperative_lease_busy')
        del f['policy']['view_evidence']; rebind_admission(f)
        with self.assertRaises(ValueError): admission(f)
        f = build_admission_fixture('cooperative_lease_busy')
        f['admission']['lease_root'] = '/tmp/different'; rebind_admission(f)
        with self.assertRaises(ValueError): admission(f)

    def test_no_start_receipt_cannot_cover_child_running_samples(self):
        f = build_admission_fixture(); f['samples'][0]['phase'] = 'running'; rebind_admission(f)
        with self.assertRaises(ValueError): admission(f)

    def test_evidence_reader_preserves_partial_and_overbound_unknown_without_link_follow(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve(); self.assertEqual(isolation.read_evidence(folder), {})
            sample = folder / 'isolation-samples.jsonl'; sample.write_bytes(b'{"partial":')
            got = isolation.read_evidence(folder)
            self.assertEqual(base64.b64decode(got['isolation_samples_bytes']), sample.read_bytes())
            with patch.object(isolation, 'MAX_BYTES', 4):
                self.assertIn('isolation_evidence_error', isolation.read_evidence(folder))
            receipt = folder / 'isolation-receipt.json'; receipt.symlink_to(sample)
            with self.assertRaises(ValueError): isolation.read_evidence(folder)

    def test_retention_bound_records_unknown_and_reserves_final_evidence(self):
        f = build_fixture()
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve(); (folder / 'execution-request.json').write_bytes(f['request_raw'])
            guard = isolation.Guard(folder, f['request'], isolation.digest(f['request_raw']))
            sample = copy.deepcopy(f['samples'][2]); sample['extra_output'] = 'x' * 8192
            try:
                with patch.object(isolation, 'MAX_BYTES', 4096), patch.object(isolation, 'MAX_SAMPLE_BYTES', 1024), \
                     patch.object(isolation, 'observe_target', return_value=sample):
                    first = guard.sample('running'); final = guard.sample('final')
                self.assertTrue(guard.stop.is_set())
                self.assertIn('capacity', guard.failure)
                self.assertEqual(first['error'], final['error'])
                self.assertIn('discarded_sample_sha256', first)
                self.assertLess((folder / 'isolation-samples.jsonl').stat().st_size, 4096)
                self.assertEqual(isolation._derive_sample(first, f['policy']), 'unknown')
                self.assertEqual(isolation._derive_sample(final, f['policy']), 'unknown')
            finally: guard.close()


if __name__ == '__main__':
    unittest.main()
