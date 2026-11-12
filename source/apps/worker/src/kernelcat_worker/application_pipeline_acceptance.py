"""Accept H only through the original, source-bound F/G/H execution receipts."""
import hashlib
import json
import math
from pathlib import Path

from kernelcat_executor.contracts.models import Conflict, digest
from kernelcat_worker.application_artifacts import read_regular, verify_evaluator


def pipeline_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


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


def original_check_result(original, path, expected_hash, actions, contexts):
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
        # Recover only that directly named owner, under the same admitted source
        # and evaluator; never re-execute an unknown check to get a new identity.
        prior = actions.get(origin_id, {})
        context = contexts.get(action['id'], {})
        prior_context = contexts.get(origin_id, {})
        if (check.get('reused') is not True or not isinstance(origin_id, str)
                or result.get('resume_run') != hashlib.sha256(origin_id.encode()).hexdigest()
                or action['arguments'].get('resume_run') != result['resume_run']
                or prior.get('action', {}).get('tool') != 'pipeline_evaluate'
                or prior.get('action', {}).get('task') != action['task']
                or any(prior_context.get(key) != context.get(key) for key in
                       ('task_version', 'evaluator_sha256', 'source_sha256'))):
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


def promoted_receipt(host, state, declared, identity, action, actions, contexts):
    evidence = action.get('outcome', {}).get('data', {}).get('promotion_evidence')
    if not isinstance(evidence, dict) or evidence.get('promotion_action_id') != identity:
        return None
    context = contexts.get(identity, {})
    if (context.get('task_version') != state['task_version'] or context.get('evaluator_sha256') != host.evaluator['sha256']
            or evidence.get('contract') != declared['pipeline_contract']
            or evidence.get('baseline_files') != declared['baseline_files']):
        return None
    snapshot = json.loads(read_regular(host.owner.gateway.store.path.parent / 'checkpoints' / (context['source_sha256'] + '.json')))
    source = {item['path']: item['sha256'] for item in snapshot['files']}
    if (set(source) != set(declared['source_paths']) or evidence.get('source_files') != source
            or evidence.get('binding', {}).get('source') != pipeline_hash(source)):
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
                or original_context.get('source_sha256') != context['source_sha256']):
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
    if (original_check_result(originals[0], Path(paths[0]), evidence['receipts'][paths[0]], actions, contexts) != raw[0]
            or original_result(originals[1], Path(paths[1]).parent.parent) != pair):
        return None
    full = raw[0]['result']
    if (any(full.get('checks', {}).get(name) is not True for name in ('build', 'semantics', 'full'))
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
    qualification = pair.get('qualification', {})
    delta, noise = qualification.get('paired_delta'), qualification.get('noise_threshold')
    if (qualification.get('qualified') is not True or type(delta) not in (int, float) or type(noise) not in (int, float)
            or not math.isfinite(delta) or not math.isfinite(noise) or noise < 0 or delta <= noise
            or type(pair.get('speedup')) not in (int, float) or not math.isfinite(pair['speedup']) or pair['speedup'] <= 1):
        return None
    return {'evidence_tier': 'E3', 'source_sha256': snapshot['source_sha256'], 'source_files': source,
        'evaluator_sha256': host.evaluator['sha256'], 'task_version': state['task_version'],
        'measurement_profile_version': digest(declared['pipeline_contract']['measurement']), 'evidence_ref': identity,
        'correct': True, 'full_suite': True, 'score': pair['speedup'], 'score_unit': 'geometric_mean_speedup',
        'paired_delta': delta, 'noise_threshold': noise, 'pair_method': 'ABBA',
        'parent_source_sha256': pipeline_hash(declared['baseline_files']), 'raw_data_ref': paths,
        'calibration': qualification, 'scope': declared['scope'], 'cases': pair['cases'], 'families': pair['families'],
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
            host.journal.command('evidence', {**receipt, 'evidence_tier': 'E1', 'evidence_ref': receipt['original_f']})
        host.journal.command('evidence', receipt)
        if host.config.application.complete_on_acceptance and state['goal'] and state['goal']['status'] == 'active':
            host.journal.command('goal_outcome', {'status': 'completed', 'evidence_ref': identity})
