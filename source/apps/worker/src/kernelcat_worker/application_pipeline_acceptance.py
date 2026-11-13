"""Accept H only through the original, source-bound F/G/H execution receipts."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re

from kernelcat_executor.contracts.models import Conflict, digest
from kernelcat_worker.application_artifacts import read_regular, read_source_snapshot, verify_evaluator


def pipeline_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def candidate_source(checkpoints, context, source_paths):
    """Project declared runtime sources from a verified full workspace checkpoint.

    Scratch files remain checkpointed for recovery, but are never promoted as
    candidate inputs. Pipeline receipts must bind exactly the declared bundle.
    """
    identity = context.get('source_sha256')
    if not isinstance(identity, str) or re.fullmatch(r'[0-9a-f]{64}', identity) is None:
        return None
    try:
        snapshot = read_source_snapshot(checkpoints / (identity + '.json'), identity)
        source = {item['path']: item['sha256'] for item in snapshot['files']}
        return {name: source[name] for name in source_paths}
    except (OSError, ValueError, KeyError, TypeError, Conflict):
        return None


def original_result(original, folder):
    """Large tool projections retain an original action-bound result manifest."""
    data = original['outcome']['data']
    inline = data.get('result')
    if isinstance(inline, dict):
        return inline
    identity = hashlib.sha256(original['action']['id'].encode()).hexdigest()
    if folder.name != identity or data.get('run') != identity:
        return None
    manifest_bytes = read_regular(folder / 'manifest.json', 4 * 1024 * 1024)
    if hashlib.sha256(manifest_bytes).hexdigest() != data.get('manifest_sha256'):
        return None
    manifest = json.loads(manifest_bytes)
    result_bytes = read_regular(folder / 'result.json', 16 * 1024 * 1024)
    if hashlib.sha256(result_bytes).hexdigest() != manifest.get('result.json'):
        return None
    result = json.loads(result_bytes)
    return result.get('data') if result.get('status') == 'completed' else None


def original_check_result(original, path, expected_hash, actions, contexts, checkpoints, source_paths):
    """Resolve F from its admitted action, including a manifest-bound composite.

    A composite's internal phases are not separate Journal admissions. Its real
    outer admission must own the F descriptor and the unchanged driver receipt.
    """
    action = original['action']
    folder = path.parent.parent
    if action['tool'] == 'pipeline_run':
        if action['arguments'].get('stage') != 'F':
            return None
        return original_result(original, folder)
    if action['tool'] != 'pipeline_evaluate':
        return None
    outer = folder.parent / hashlib.sha256(action['id'].encode()).hexdigest()
    result = original_result(original, outer)
    if not isinstance(result, dict):
        return None
    stages = result.get('stages', [])
    checks = [row for row in stages if isinstance(row, dict) and row.get('stage') == 'F']
    if len(checks) != 1:
        return None
    check = checks[0]
    internal_id = check.get('action_id')
    origin_id = check.get('origin_action_id', action['id'])
    if origin_id != action['id']:
        # A completed internal F can survive a crash before its outer reply.
        # Recover only that directly named owner, under the same admitted
        # candidate and evaluator; unrelated scratch changes do not re-run F.
        prior = actions.get(origin_id, {})
        context = contexts.get(action['id'], {})
        prior_context = contexts.get(origin_id, {})
        if (check.get('reused') is not True or not isinstance(origin_id, str)
                or result.get('resume_run') != hashlib.sha256(origin_id.encode()).hexdigest()
                or action['arguments'].get('resume_run') != result['resume_run']
                or prior.get('action', {}).get('tool') != 'pipeline_evaluate'
                or prior.get('action', {}).get('task') != action['task']
                or any(prior_context.get(key) != context.get(key) for key in
                       ('task_version', 'evaluator_sha256'))):
            return None
        source = candidate_source(checkpoints, context, source_paths)
        if source is None or candidate_source(checkpoints, prior_context, source_paths) != source:
            return None
    if (check.get('tool') != 'pipeline_run' or check.get('status') != 'completed'
            or not isinstance(internal_id, str)
            or not internal_id.startswith(origin_id + '/phase/')
            or check.get('run') != hashlib.sha256(internal_id.encode()).hexdigest()
            or folder.name != check['run'] or path != folder / 'driver/receipt.json'):
        return None
    receipt = check.get('receipt', {})
    if receipt.get('path') != str(path) or receipt.get('sha256') != expected_hash:
        return None
    manifest_bytes = read_regular(folder / 'manifest.json', 4 * 1024 * 1024)
    if hashlib.sha256(manifest_bytes).hexdigest() != check.get('manifest_sha256'):
        return None
    if json.loads(manifest_bytes).get('driver/receipt.json') != expected_hash:
        return None
    return result.get('check_receipt')


def original_isolation(host, declared, original_g, pair, raw, source):
    """Recompute isolation from pinned code and the original G artifact bytes."""
    requirement = declared['pipeline_contract']['measurement'].get('device_isolation')
    if requirement is None:
        return None
    action = original_g['action']
    run = hashlib.sha256(action['id'].encode()).hexdigest()
    folder = Path(pair['receipts'][0]).parent.parent
    if folder.name != run:
        raise ValueError('Isolation evidence is not the original G directory')
    manifest_raw = read_regular(folder / 'manifest.json', 4 * 1024 * 1024)
    if hashlib.sha256(manifest_raw).hexdigest() != original_g['outcome']['data'].get('manifest_sha256'):
        raise ValueError('Original G isolation manifest changed')
    manifest = json.loads(manifest_raw)
    # The selected configuration and helper are evaluator inputs, not model
    # parameters or paths supplied by an isolation result.
    configurations = []
    for name, expected in host.evaluator['files'].items():
        path = Path(name)
        if path.name != 'config.json':
            continue
        content = read_regular(path, 4 * 1024 * 1024)
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('Pinned pipeline configuration changed')
        config = json.loads(content)
        if isinstance(config, dict) and config.get('state') == str(folder.parent):
            configurations.append((path, config))
    if len(configurations) != 1:
        raise ValueError('Isolation requires the unique original pinned pipeline configuration')
    config_path, config = configurations[0]
    if pipeline_hash(config.get('device_isolation')) != pipeline_hash(requirement):
        raise ValueError('Host isolation policy differs from the frozen pipeline configuration')
    driver_name = action['arguments'].get('driver')
    driver = config['drivers'].get(driver_name)
    if not driver or any(arm.get('driver') != driver_name for arm in raw):
        raise ValueError('Isolation validator differs from the original G driver')
    # Driver paths/hashes are transitively fixed by the evaluator-pinned config;
    # the shared consumer rehashes the full closure before loading its validator.
    for index, arm in enumerate(raw):
        names = ['receipt.json', *(arm['result'].get('device_isolation') or {}).get('artifacts', {})]
        for name in names:
            relative = 'arm-' + str(index) + '/' + name
            path = folder / relative
            if path.resolve() != path or not path.is_relative_to(folder):
                raise ValueError('Isolation artifact escaped the original G directory')
            if hashlib.sha256(read_regular(path)).hexdigest() != manifest.get(relative):
                raise ValueError('Original G isolation bytes changed')
    helper = config_path.with_name('isolation.py')
    expected = host.evaluator['files'].get(str(helper))
    helper_raw = read_regular(helper)
    if not expected or hashlib.sha256(helper_raw).hexdigest() != expected:
        raise ValueError('Isolation consumer is not a pinned host evaluator input')
    spec = importlib.util.spec_from_file_location('kernelbridge_original_isolation_' + expected, helper)
    module = importlib.util.module_from_spec(spec)
    exec(compile(helper_raw, str(helper), 'exec'), module.__dict__)
    proof = module.verify([arm['result'] for arm in raw], declared['pipeline_contract']['measurement'], source,
                          run_folder=folder, driver=driver, configured=config['device_isolation'])
    if pipeline_hash(proof) != pipeline_hash(pair.get('qualification', {}).get('device_isolation')):
        raise ValueError('Original isolation qualification changed')
    return proof


def promoted_receipt(host, state, declared, identity, action, actions, contexts):
    evidence = action.get('outcome', {}).get('data', {}).get('promotion_evidence')
    if not isinstance(evidence, dict) or evidence.get('promotion_action_id') != identity:
        return None
    context = contexts.get(identity, {})
    if (context.get('task_version') != state['task_version'] or context.get('evaluator_sha256') != host.evaluator['sha256']
            or evidence.get('contract') != declared['pipeline_contract']
            or evidence.get('baseline_files') != declared['baseline_files']):
        return None
    checkpoints = host.owner.gateway.store.path.parent / 'checkpoints'
    source = candidate_source(checkpoints, context, declared['source_paths'])
    if (source is None or evidence.get('source_files') != source
            or evidence.get('binding', {}).get('source') != pipeline_hash(source)
            or evidence.get('binding', {}).get('baseline') != pipeline_hash(declared['baseline_files'])):
        return None
    check, pair = evidence.get('check', {}), evidence.get('pair', {})
    if (not check or not pair or check.get('passed') is not True or check.get('scope') != declared['scope']
            or check.get('binding') != evidence['binding'] or pair.get('binding') != evidence['binding']):
        return None
    originals = []
    for record, tools in ((check, {'pipeline_run', 'pipeline_evaluate'}), (pair, {'pipeline_pair'})):
        original_id = record.get('action_id')
        original = actions.get(original_id, {})
        original_context = contexts.get(original_id, {})
        if (not original.get('terminal') or original.get('outcome', {}).get('status') != 'completed'
                or original.get('action', {}).get('tool') not in tools or original.get('action', {}).get('task') != host.task.id
                or original_context.get('task_version') != state['task_version']
                or original_context.get('evaluator_sha256') != host.evaluator['sha256']
                or candidate_source(checkpoints, original_context, declared['source_paths']) != source):
            return None
        originals.append(original)
    paths = [check['receipt'], *pair.get('receipts', [])]
    if len(paths) != 5 or set(evidence.get('receipts', {})) != set(paths):
        return None
    raw = []
    for path in paths:
        content = read_regular(Path(path), 4 * 1024 * 1024)
        if hashlib.sha256(content).hexdigest() != evidence['receipts'][path]:
            return None
        raw.append(json.loads(content))
    # Do not accept a copied external report as an original tool result.
    if (original_check_result(originals[0], Path(paths[0]), evidence['receipts'][paths[0]], actions, contexts,
                              checkpoints, declared['source_paths']) != raw[0]
            or original_result(originals[1], Path(paths[1]).parent.parent) != pair):
        return None
    full = raw[0]['result']
    if (raw[0].get('source') != pipeline_hash(source) or raw[0].get('contract') != evidence['binding'].get('contract')
            or any(full.get('checks', {}).get(name) is not True for name in ('build', 'semantics', 'full'))
            or full.get('case_count') != len(declared['pipeline_contract']['suite'])
            or sorted(full.get('case_ids', [])) != sorted(case['id'] for case in declared['pipeline_contract']['suite'])
            or full.get('oracle_sha256') != pipeline_hash(declared['pipeline_contract']['oracle']['files'])):
        return None
    for arm, expected in zip(raw[1:], (declared['baseline_files'], source, source, declared['baseline_files']), strict=True):
        if (arm.get('source') != pipeline_hash(expected) or arm.get('contract') != raw[0].get('contract')
                or arm.get('result', {}).get('correct') is not True
                or arm.get('result', {}).get('measurement') != declared['pipeline_contract']['measurement']
                or arm.get('result', {}).get('environment') != pair.get('environment')):
            return None
    isolation = original_isolation(host, declared, originals[1], pair, raw[1:], source)
    qualification = pair.get('qualification', {})
    delta, noise = qualification.get('paired_delta'), qualification.get('noise_threshold')
    if (qualification.get('qualified') is not True or type(delta) not in (int, float) or type(noise) not in (int, float)
            or not math.isfinite(delta) or not math.isfinite(noise) or noise < 0 or delta <= noise
            or type(pair.get('speedup')) not in (int, float) or not math.isfinite(pair['speedup']) or pair['speedup'] <= 1):
        return None
    return {'evidence_tier': 'E3', 'source_sha256': context['source_sha256'], 'source_files': source,
        'evaluator_sha256': host.evaluator['sha256'], 'task_version': state['task_version'],
        'measurement_profile_version': digest(declared['pipeline_contract']['measurement']), 'evidence_ref': identity,
        'correct': True, 'full_suite': True, 'score': pair['speedup'], 'score_unit': 'geometric_mean_speedup',
        'paired_delta': delta, 'noise_threshold': noise, 'pair_method': 'ABBA',
        'parent_source_sha256': pipeline_hash(declared['baseline_files']), 'raw_data_ref': paths,
        'calibration': qualification, 'scope': declared['scope'], 'cases': pair['cases'], 'families': pair['families'],
        **({'device_isolation': isolation} if isolation is not None else {}),
        'regressions': pair['regressions'], 'original_f': check['action_id'], 'original_g': pair['action_id'],
        'receipt_hashes': evidence['receipts']}


def collect_pipeline_acceptance(host, state, declared):
    verify_evaluator(host.evaluator)
    events = list(host.owner.gateway.store._events(host.owner.gateway.store.events()))
    contexts = {row['action']['id']: row.get('application_context', {}) for row in events if row['kind'] == 'admitted'}
    actions = host.owner.gateway.store.snapshot()['actions']
    known = {item['evidence_ref'] for item in state['evidence']}
    for identity, action in actions.items():
        if (identity in known or not action.get('terminal') or action['action']['tool'] != declared['tool']
                or action['action']['task'] != host.task.id or action.get('outcome', {}).get('status') != 'completed'):
            continue
        try:
            receipt = promoted_receipt(host, state, declared, identity, action, actions, contexts)
        except (OSError, ValueError, KeyError, TypeError, Conflict):
            continue  # Original failed/mismatched evidence remains inspectable; never reinterpret it.
        if receipt is None:
            continue
        if receipt['original_f'] not in known:
            host.journal.command('evidence', {**receipt, 'evidence_tier': 'E1', 'evidence_ref': receipt['original_f'],
                'source_sha256': contexts[receipt['original_f']]['source_sha256']})
        host.journal.command('evidence', receipt)
        if host.config.application.complete_on_acceptance and state['goal'] and state['goal']['status'] == 'active':
            host.journal.command('goal_outcome', {'status': 'completed', 'evidence_ref': identity})
