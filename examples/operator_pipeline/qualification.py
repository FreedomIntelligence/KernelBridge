"""Declared ABBA aggregation and independently measured log-noise qualification.

The adapter owns execution and verifies original device receipts. This module
checks its complete raw projections again; it never launches or estimates data.
"""
import hashlib
import json
import math
import statistics


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('Timing samples must be positive finite measurements')
    return value


def aggregate(results, measurement):
    """Preserve legacy profiles; explicit protocols use their declared statistic."""
    ids = sorted(results[0]['cases'])
    for result in results:
        for samples in result['cases'].values():
            if not isinstance(samples, list) or not samples:
                raise ValueError('Nonempty timing samples required')
            for sample in samples: positive(sample)
    method = measurement.get('aggregation')
    if method == 'pooled_arm_median':
        return {case: statistics.median(results[0]['cases'][case] + results[3]['cases'][case]) /
                statistics.median(results[1]['cases'][case] + results[2]['cases'][case]) for case in ids}
    if method == 'paired_block_log_mean':
        count = measurement.get('pairs')
        if type(count) is not int or count < 2 or any(len(result['cases'][case]) != count for result in results for case in ids):
            raise ValueError('Declared paired block count differs from samples')
        return {case: math.exp(statistics.mean(math.log(
            (results[0]['cases'][case][block] + results[3]['cases'][case][block]) /
            (results[1]['cases'][case][block] + results[2]['cases'][case][block])) for block in range(count))) for case in ids}
    # Profiles predating explicit aggregation retain their original behavior.
    medians = [{case: statistics.median(result['cases'][case]) for case in ids} for result in results]
    return {case: (medians[0][case] + medians[3][case]) / (medians[1][case] + medians[2][case]) for case in ids}


def _blocks(cases, ids, count, roles, repeats):
    if not isinstance(cases, list) or len(cases) != len(ids) or {case.get('id') for case in cases} != set(ids):
        raise ValueError('Raw pairing requires each declared case exactly once')
    blocks = {}
    identities = {}
    for case in cases:
        arms = case.get('arms')
        if not isinstance(arms, list) or len(arms) != 4 * count:
            raise ValueError('Raw pairing has incomplete blocks')
        case_id = case['id']; blocks[case_id] = {}; identities[case_id] = {}
        for arm in arms:
            pair, position = arm.get('pair'), arm.get('position')
            if type(pair) is not int or type(position) is not int or pair not in range(count) or position not in range(4):
                raise ValueError('Raw pairing has invalid block/position')
            key = (pair, position)
            if key in blocks[case_id] or arm.get('arm') != roles[position]:
                raise ValueError('Raw pairing has duplicate or reordered arms')
            blocks[case_id][key] = positive(arm.get('us'))
            if 'samples_us' in arm:
                samples = arm['samples_us']
                if not isinstance(samples, list) or len(samples) != repeats:
                    raise ValueError('Raw arm has incomplete event timings')
                for value in samples: positive(value)
                if not math.isclose(statistics.mean(samples), arm['us'], rel_tol=1e-10, abs_tol=1e-12):
                    raise ValueError('Raw event mean differs from reported arm timing')
            input_hash = arm.get('input_sha256', case.get('input_sha256'))
            if not isinstance(input_hash, str) or len(input_hash) != 64 or any(c not in '0123456789abcdef' for c in input_hash):
                raise ValueError('Raw pairing lacks input identity')
            identities[case_id][key] = (input_hash, arm.get('seed'))
        for pair in range(count):
            if len({identities[case_id][(pair, pos)] for pos in range(4)}) != 1:
                raise ValueError('Inputs differ within a paired block')
    return blocks, identities


def _differences(blocks, ids, count):
    return [statistics.mean(math.log((blocks[case][(block, 0)] + blocks[case][(block, 3)]) /
            (blocks[case][(block, 1)] + blocks[case][(block, 2)])) for case in ids) for block in range(count)]


def qualify(results, contract, binding, source_files, baseline_files, ratios):
    """Return E3 qualification only for an explicitly noise-qualified protocol."""
    measurement = contract['measurement']
    if measurement.get('require_noise') is not True:
        return None
    if measurement.get('aggregation') not in {'pooled_arm_median', 'paired_block_log_mean'}:
        raise ValueError('Noise qualification requires a predeclared aggregation')
    protocol = measurement.get('protocol_id')
    count = measurement.get('pairs')
    if not isinstance(protocol, str) or not protocol or type(count) is not int or count < 2:
        raise ValueError('Noise qualification requires a named repeated paired protocol')
    pairing = results[0].get('pairing')
    if not isinstance(pairing, dict) or any(result.get('pairing') != pairing for result in results):
        raise ValueError('All ABBA projections must retain identical original raw pairing')
    expected = {key: binding[key] for key in ('source', 'baseline', 'contract')}
    expected.update(oracle=contract['oracle']['files']['sha256'], protocol=protocol, environment=results[0]['environment'])
    declared = pairing.get('binding')
    if not isinstance(declared, dict) or any(declared.get(key) != value for key, value in expected.items()):
        raise ValueError('Raw pairing source/baseline/oracle/protocol/environment binding differs')
    oracle_hashes = set(contract['oracle']['files']['files'].values())
    if declared.get('evaluator') not in oracle_hashes:
        raise ValueError('Raw pairing evaluator is not pinned by the oracle contract')
    calibration = pairing.get('calibration')
    if not isinstance(calibration, dict) or calibration.get('binding') != declared:
        raise ValueError('Missing or mismatched independent noise calibration binding')
    if calibration.get('schema_version') != 'kernelbridge.noise-calibration/v1' or calibration.get('protocol') != 'independent_eager_aaaa':
        raise ValueError('Independent eager/eager AAAA noise measurements are required')
    for key in ('pairs', 'warmup', 'repeats', 'device'):
        if calibration.get(key) != measurement.get(key):
            raise ValueError('Calibration measurement differs: ' + key)
    if calibration.get('source_sha256') not in source_files.values() or calibration.get('baseline_sha256') not in baseline_files.values() or calibration.get('driver_sha256') != declared['evaluator']:
        raise ValueError('Calibration source/baseline/evaluator identity differs')
    environment = calibration.get('environment')
    if not isinstance(environment, dict) or not environment or any(results[0]['environment'].get(key) != value for key, value in environment.items()):
        raise ValueError('Calibration device environment differs')
    receipt = calibration.get('receipt')
    payload = {key: value for key, value in calibration.items() if key not in {'binding', 'receipt'}}
    if not isinstance(receipt, dict) or not receipt.get('path') or receipt.get('payload_sha256') != digest(payload):
        raise ValueError('Independent calibration raw receipt is missing or changed')
    file_hash = hashlib.sha256((json.dumps(payload, indent=2, allow_nan=False) + '\n').encode()).hexdigest()
    if receipt.get('sha256') != file_hash:
        raise ValueError('Independent calibration file hash differs from raw payload')
    ids = sorted(ratios)
    blocks, inputs = _blocks(pairing.get('cases'), ids, count, ('eager', 'candidate', 'candidate', 'eager'), measurement['repeats'])
    null, null_inputs = _blocks(calibration.get('cases'), ids, count, ('eager',) * 4, measurement['repeats'])
    if inputs != null_inputs:
        raise ValueError('Noise calibration inputs differ from candidate pairing')
    for position, result in enumerate(results):
        if result.get('original_arm_index') != position:
            raise ValueError('ABBA projection lost original arm position')
        for case in ids:
            if result['cases'][case] != [blocks[case][(block, position)] for block in range(count)]:
                raise ValueError('ABBA projection differs from original raw measurements')
    summary = results[0].get('original_aggregation')
    if summary is not None:
        if any(result.get('original_aggregation') != summary for result in results) or set(summary.get('speedups', {})) != set(ids):
            raise ValueError('Original reported aggregation differs across projections')
        for case in ids:
            if not math.isclose(positive(summary['speedups'][case]), ratios[case], rel_tol=1e-10):
                raise ValueError('Declared aggregation changes the original reported case speedup')
    delta_blocks = _differences(blocks, ids, count)
    null_blocks = _differences(null, ids, count)
    delta = statistics.mean(delta_blocks)
    theta = abs(statistics.mean(null_blocks)) + 3 * statistics.stdev(null_blocks) / math.sqrt(count)
    overall = math.exp(statistics.mean(math.log(value) for value in ratios.values()))
    if summary is not None and not math.isclose(positive(summary.get('geomean_speedup')), overall, rel_tol=1e-10):
        raise ValueError('Declared aggregation changes the original reported geomean')
    return {'qualified': overall > 1 and delta > theta, 'paired_delta': delta, 'noise_threshold': theta,
            'reported_geomean': overall, 'method': 'independent-aaaa-mean-log-3se-v1', 'pair_method': 'ABBA',
            'scope': measurement.get('scope'), 'protocol_id': protocol, 'aggregation': measurement['aggregation'],
            'binding': declared, 'paired_block_deltas': delta_blocks, 'null_block_deltas': null_blocks,
            'calibration_ref': receipt, 'raw_pairing_sha256': digest(pairing),
            'calibration_scope': 'independent eager/eager timings; no holdout or independent candidate confirmation claim'}
