"""Source-bound E3 tests using synthetic timings, never device evidence."""
import copy
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
sys.path.insert(0, str(ROOT / 'validation'))
from qualification import aggregate, digest, qualify, read_calibration_artifacts
from pipeline import Pipeline
import test_operator_pipeline as pipeline_tests


class QualificationTests(unittest.TestCase):
    def fixture(self, a=100., b=80., null_a=100., null_b=100.):
        source, baseline, evaluator = 'a' * 64, 'b' * 64, 'c' * 64
        measurement = {'aggregation': 'pooled_arm_median', 'require_noise': True, 'protocol_id': 'test-abba-v1',
                       'pairs': 3, 'repeats': 2, 'warmup': 3, 'device': 0, 'scope': 'synthetic_fixture'}
        contract = {'measurement': measurement, 'suite': [{'id': 'fast', 'family': 'fast'}, {'id': 'slow', 'family': 'slow'}],
                    'oracle': {'files': {'sha256': 'd' * 64, 'files': {'driver.py': evaluator, 'eager.py': baseline}}}}
        binding = {'source': 'e' * 64, 'baseline': 'f' * 64, 'contract': '1' * 64}
        env = {'device': 'synthetic-no-device', 'device_index': 0}
        identity = dict(binding, oracle=contract['oracle']['files']['sha256'], evaluator=evaluator,
                        protocol=measurement['protocol_id'], environment=env)
        def cases(left, right, null=False):
            result = []
            for name in ('fast', 'slow'):
                arms = []
                for pair in range(3):
                    for position in range(4):
                        value = left if position in (0, 3) else right
                        # Keep a regressing family in a positive overall result.
                        if name == 'slow' and not null and b != a: value = 100. if position in (0, 3) else 101.
                        arms.append({'pair': pair, 'position': position, 'arm': 'eager' if null or position in (0, 3) else 'candidate', 'us': value})
                result.append({'id': name, 'input_sha256': '2' * 64, 'arms': arms})
            return result
        raw = cases(a, b)
        noise = {'schema_version': 'kernelbridge.noise-calibration/v1', 'protocol': 'independent_eager_aaaa',
                 'pairs': 3, 'repeats': 2, 'warmup': 3, 'device': 0, 'source_sha256': source,
                 'baseline_sha256': baseline, 'driver_sha256': evaluator, 'environment': env,
                 'cases': cases(null_a, null_b, True)}
        calibration = dict(noise, binding=identity, receipt={'path': '/synthetic/noise.json', 'payload_sha256': digest(noise),
                      'sha256': hashlib.sha256((json.dumps(noise, indent=2, allow_nan=False) + '\n').encode()).hexdigest()})
        self.artifact_bytes = getattr(self, 'artifact_bytes', {})
        self.artifact_bytes[calibration['receipt']['sha256']] = (json.dumps(noise, indent=2, allow_nan=False) + '\n').encode()
        pairing = {'binding': identity, 'cases': raw, 'calibration': calibration}
        results = [{'cases': {case['id']: [case['arms'][block * 4 + pos]['us'] for block in range(3)] for case in raw},
                    'environment': env, 'measurement': measurement, 'correct': True, 'pairing': pairing,
                    'original_arm_index': pos} for pos in range(4)]
        return results, contract, binding, {'candidate.py': source}, {'eager.py': baseline}

    def qualify(self, fixture):
        results, contract, *rest = fixture
        ratios = aggregate(results, contract['measurement'])
        receipt = results[0].get('pairing', {}).get('calibration', {}).get('receipt', {})
        artifacts = [self.artifact_bytes.get(receipt.get('sha256'), b'')] * 4
        return qualify(results, contract, *rest, ratios, calibration_artifacts=artifacts)

    def reseal(self, fixture):
        calibration = fixture[0][0]['pairing']['calibration']
        raw = {k: v for k, v in calibration.items() if k not in {'binding', 'receipt'}}
        calibration['receipt']['payload_sha256'] = digest(raw)
        calibration['receipt']['sha256'] = hashlib.sha256((json.dumps(raw, indent=2, allow_nan=False) + '\n').encode()).hexdigest()
        self.artifact_bytes[calibration['receipt']['sha256']] = (json.dumps(raw, indent=2, allow_nan=False) + '\n').encode()

    def test_scatter_preserves_pooled_six_sample_medians(self):
        results = [{'cases': {'case': x}} for x in ([1, 1, 100], [10] * 3, [10] * 3, [100] * 3)]
        self.assertEqual(aggregate(results, {'aggregation': 'pooled_arm_median'})['case'], 10)
        self.assertEqual(aggregate(results, {})['case'], 5.05)

    def test_cube_declared_block_log_mean(self):
        results = [{'cases': {'case': x}} for x in ([2, 8], [1, 1], [3, 3], [6, 8])]
        self.assertAlmostEqual(aggregate(results, {'aggregation': 'paired_block_log_mean', 'pairs': 2})['case'], math.sqrt(8))

    def test_qualified_retains_regression_and_no_holdout_claim(self):
        fixture = self.fixture()
        result = self.qualify(fixture)
        self.assertTrue(result['qualified']); self.assertEqual(result['noise_threshold'], 0.)
        self.assertLess(aggregate(fixture[0], fixture[1]['measurement'])['slow'], 1)
        self.assertNotIn('holdout', result); self.assertNotIn('independent_confirmation', result)

    def test_delta_equal_noise_is_rejected_even_if_geomean_positive(self):
        fixture = self.fixture(a=100., b=100., null_a=100., null_b=80.)
        # Both cases have a precise log(1.25) candidate delta and null shift.
        for case in fixture[0][0]['pairing']['cases']:
            for row in case['arms']:
                if row['position'] in (1, 2): row['us'] = 80.
        for pos, result in enumerate(fixture[0]):
            result['cases'] = {case['id']: [case['arms'][block * 4 + pos]['us'] for block in range(3)] for case in result['pairing']['cases']}
        result = self.qualify(fixture)
        self.assertEqual(result['paired_delta'], result['noise_threshold'])
        self.assertGreater(result['reported_geomean'], 1)
        self.assertFalse(result['qualified'])

    def test_noise_uses_full_suite_block_means_and_three_standard_errors(self):
        fixture = self.fixture(); noise = fixture[0][0]['pairing']['calibration']
        for case in noise['cases']:
            for arm in case['arms']:
                arm['us'] = 100. * (1 + .1 * arm['pair']) if arm['position'] in (0, 3) else 100.
        self.reseal(fixture)
        result = self.qualify(fixture)
        d = [math.log(1), math.log(1.1), math.log(1.2)]
        self.assertAlmostEqual(result['noise_threshold'], abs(statistics.mean(d)) + 3 * statistics.stdev(d) / math.sqrt(3))

    def test_missing_noise_and_every_binding_mismatch_fail_closed(self):
        fixture = self.fixture(); fixture[0][0]['pairing'].pop('calibration')
        with self.assertRaises(ValueError): self.qualify(fixture)
        for key in ('source', 'baseline', 'contract', 'oracle', 'evaluator', 'protocol', 'environment'):
            with self.subTest(binding=key):
                fixture = self.fixture()
                fixture[0][0]['pairing']['calibration']['binding'] = dict(fixture[0][0]['pairing']['binding'], **{key: 'wrong'})
                with self.assertRaises(ValueError): self.qualify(fixture)

    def test_native_calibration_identity_and_receipt_tamper_fail(self):
        for key in ('source_sha256', 'baseline_sha256', 'driver_sha256', 'device', 'repeats', 'environment'):
            with self.subTest(identity=key):
                fixture = self.fixture(); fixture[0][0]['pairing']['calibration'][key] = 'wrong'; self.reseal(fixture)
                with self.assertRaises(ValueError): self.qualify(fixture)
        fixture = self.fixture(); fixture[0][0]['pairing']['calibration']['receipt']['sha256'] = '0' * 64
        with self.assertRaises(ValueError): self.qualify(fixture)

    def test_missing_duplicate_malformed_blocks_or_inputs_fail(self):
        for calibration in (False, True):
            for mutation in ('missing', 'duplicate', 'zero', 'nan', 'inputs'):
                with self.subTest(calibration=calibration, mutation=mutation):
                    fixture = self.fixture(); pairing = fixture[0][0]['pairing']
                    cases = pairing['calibration']['cases'] if calibration else pairing['cases']
                    arms = cases[0]['arms']
                    if mutation == 'missing': arms.pop()
                    elif mutation == 'duplicate': arms[1] = copy.deepcopy(arms[0])
                    elif mutation == 'inputs': arms[0]['input_sha256'] = '3' * 64
                    else: arms[0]['us'] = 0 if mutation == 'zero' else float('nan')
                    if calibration and mutation != 'nan': self.reseal(fixture)
                    with self.assertRaises(ValueError): self.qualify(fixture)

    def test_raw_projection_and_original_summary_cannot_change_statistic(self):
        fixture = self.fixture(); fixture[0][0]['cases']['fast'][0] += 1
        with self.assertRaises(ValueError): self.qualify(fixture)
        fixture = self.fixture()
        for result in fixture[0]: result['original_aggregation'] = {'speedups': {'fast': 99, 'slow': 99}, 'geomean_speedup': 99}
        with self.assertRaises(ValueError): self.qualify(fixture)

    def test_real_adapter_envelope_round_trips_raw_noise(self):
        sys.path.insert(0, str(ROOT / 'examples/scatter'))
        try:
            from npu_pipeline_driver import pairing_record
        finally:
            sys.path.pop(0)
        fixture = self.fixture(); results, contract, binding, _, _ = fixture
        pairing = results[0]['pairing']; calibration = pairing['calibration']
        noise = {k: v for k, v in calibration.items() if k not in {'binding', 'receipt'}}
        native = {k: noise[k] for k in ('pairs', 'warmup', 'repeats', 'device', 'baseline_sha256', 'driver_sha256', 'source_sha256', 'environment')}
        native.update(cases=pairing['cases'], noise_calibration=noise, noise_calibration_sha256=calibration['receipt']['sha256'])
        with tempfile.TemporaryDirectory() as output:
            projected = pairing_record({'binding': binding, 'contract': contract, 'output': output}, native,
                                       {'original_job': '/synthetic'}, results[0]['environment'])
            self.assertEqual(projected, pairing)
            self.assertEqual(hashlib.sha256((Path(output) / 'noise.json').read_bytes()).hexdigest(), calibration['receipt']['sha256'])
            for result in results: result['pairing'] = projected
            self.assertTrue(self.qualify(fixture)['qualified'])

    def test_fixed_local_calibration_resolver_rejects_missing_or_symlinked_artifact(self):
        fixture = self.fixture(); receipt = fixture[0][0]['pairing']['calibration']['receipt']
        raw = self.artifact_bytes[receipt['sha256']]
        with tempfile.TemporaryDirectory() as output:
            root = Path(output).resolve()
            for position in range(4):
                folder = root / ('arm-' + str(position)); folder.mkdir()
                (folder / 'noise.json').write_bytes(raw)
            self.assertEqual(read_calibration_artifacts(root), [raw] * 4)
            missing = root / 'arm-3/noise.json'; missing.unlink()
            with self.assertRaises(ValueError): read_calibration_artifacts(root)
            missing.symlink_to(root / 'arm-0/noise.json')
            with self.assertRaises(ValueError): read_calibration_artifacts(root)

    def test_canonical_receipt_replay_keeps_original_file_identity(self):
        fixture = self.fixture(); expected = self.qualify(fixture)
        restored = json.loads(json.dumps(fixture, sort_keys=True))
        self.assertEqual(self.qualify(restored), expected)

    def test_noise_requires_original_bytes_and_rejects_changed_format(self):
        fixture = self.fixture(); results, contract, *rest = fixture
        ratios = aggregate(results, contract['measurement'])
        self.assertEqual(qualify(results, contract, *rest, ratios), self.qualify(fixture))
        reordered_results = json.loads(json.dumps(results, sort_keys=True))
        with self.assertRaisesRegex(ValueError, 'original file hash'):
            qualify(reordered_results, contract, *rest, ratios)
        receipt = results[0]['pairing']['calibration']['receipt']
        raw = self.artifact_bytes[receipt['sha256']]
        reordered = (json.dumps(json.loads(raw), sort_keys=True, indent=2) + '\n').encode()
        self.assertNotEqual(raw, reordered)
        with self.assertRaisesRegex(ValueError, 'original file hash'):
            qualify(results, contract, *rest, ratios, calibration_artifacts=[reordered] * 4)
        with self.assertRaisesRegex(ValueError, 'original file hash'):
            qualify(results, contract, *rest, ratios, calibration_artifacts=[raw, raw, raw, raw + b' '])

    def test_resealed_bytes_cannot_change_payload_or_hide_duplicate_fields(self):
        for mutation in ('payload', 'duplicate', 'nonfinite'):
            with self.subTest(mutation=mutation):
                fixture = self.fixture(); results, contract, *rest = fixture
                calibration = results[0]['pairing']['calibration']; receipt = calibration['receipt']
                raw = self.artifact_bytes[receipt['sha256']]
                if mutation == 'payload':
                    document = json.loads(raw); document['cases'][0]['arms'][0]['us'] += 1
                    changed = json.dumps(document).encode()
                elif mutation == 'duplicate':
                    changed = b'{"pairs":3,' + raw[1:]
                else:
                    changed = b'{"invalid":NaN}'
                receipt['sha256'] = hashlib.sha256(changed).hexdigest()
                with self.assertRaises(ValueError):
                    qualify(results, contract, *rest, aggregate(results, contract['measurement']),
                            calibration_artifacts=[changed] * 4)

    def test_legacy_profile_does_not_require_calibration(self):
        fixture = self.fixture(); fixture[1]['measurement'].pop('require_noise'); fixture[0][0].pop('pairing')
        self.assertIsNone(self.qualify(fixture))

    def test_full_pipeline_preserves_regressions_but_rejects_insufficient_e3(self):
        harness = pipeline_tests.OperatorPipelineTests(methodName='runTest'); harness.setUp(); self.addCleanup(harness.tearDown)
        fixture = list(self.fixture(a=100., b=80., null_a=100., null_b=50.))
        contract = copy.deepcopy(harness.contract)
        contract['measurement'] = fixture[1]['measurement']; contract['suite'] = fixture[1]['suite']
        harness.call('pipeline_bind', contract=contract); harness.freeze(); harness.baseline()
        harness.fixture_driver('check_ok', 'check'); harness.call('pipeline_run', stage='F', driver='check_ok')
        pipeline = Pipeline(harness.config)
        # This integration supplies already checked synthetic adapter receipts.
        fixture[1] = pipeline.state['contract']; fixture[2] = pipeline.binding()
        fixture[3] = pipeline.current()['files']; fixture[4] = pipeline.state['baseline']['files']
        identity = fixture[0][0]['pairing']['binding']
        identity.update({k: fixture[2][k] for k in ('source', 'baseline', 'contract')})
        identity['oracle'] = fixture[1]['oracle']['files']['sha256']
        identity['evaluator'] = next(iter(fixture[1]['oracle']['files']['files'].values()))
        noise = fixture[0][0]['pairing']['calibration']
        noise['source_sha256'] = next(iter(fixture[3].values())); noise['baseline_sha256'] = next(iter(fixture[4].values())); noise['driver_sha256'] = identity['evaluator']
        self.reseal(fixture)
        harness.config['drivers']['fixture_measure'] = {'kind': 'measure'}
        iterator = iter(fixture[0])
        def retained_driver(name, source, folder, role):
            result = next(iterator); folder.mkdir()
            receipt = result['pairing']['calibration']['receipt']
            (folder / 'noise.json').write_bytes(self.artifact_bytes[receipt['sha256']])
            return {'result': result}
        pipeline.driver = retained_driver
        with tempfile.TemporaryDirectory() as output:
            paired = pipeline.execute('pipeline_pair', {'driver': 'fixture_measure'}, Path(output).resolve())
            self.assertEqual(paired['regressions'], ['slow']); self.assertLess(paired['families']['slow'], 1)
            self.assertGreater(paired['speedup'], 1); self.assertFalse(paired['qualification']['qualified'])
            self.assertEqual(pipeline.workflow()['next_actions'][0]['stage'], 'C')
            with self.assertRaisesRegex(ValueError, 'above measured noise'):
                pipeline.execute('pipeline_promote', {}, Path(output))


if __name__ == '__main__': unittest.main()
