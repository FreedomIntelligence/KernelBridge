"""One bounded D/E/F call; every effect retains its original phase identity.

This composes the existing pipeline transitions, never admits model work, grants
promotion, or replaces an uncertain execution. The caller owns the pipeline lock.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import time

from pipeline import Pipeline, digest, encoded, save, sha


def run_id(action_id):
    return hashlib.sha256(action_id.encode()).hexdigest()


def phase_record(pipeline, run, stage, action_id, status):
    record = next(row for row in pipeline.state['receipts'] if row['run'] == run)
    folder = pipeline.root / run
    if sha(folder / 'manifest.json') != record['manifest_sha256']:
        raise ValueError('Original phase manifest changed')
    files = json.loads((folder / 'manifest.json').read_text())
    result_path = folder / 'result.json'
    if sha(result_path) != files.get('result.json'):
        raise ValueError('Original phase result changed')
    original = json.loads(result_path.read_text())
    if original.get('status') != record['status'] or status != record['status']:
        raise ValueError('Original phase status differs from its committed receipt')
    request = folder / 'driver/request.json'
    if request.exists() and sha(request) != files.get('driver/request.json'):
        raise ValueError('Original phase request changed')
    row = {'stage': stage, 'tool': record['tool'], 'action_id': action_id, 'run': run,
           'status': original['status'], 'manifest_sha256': record['manifest_sha256'],
           'result': {'path': str(result_path), 'sha256': files['result.json'],
                      'read': {'tool': 'pipeline_read', 'arguments': {'run': run, 'path': 'result.json'}}}}
    receipt = folder / 'driver/receipt.json'
    if receipt.exists():
        if sha(receipt) != files.get('driver/receipt.json'):
            raise ValueError('Original phase driver receipt changed')
        timing = json.loads(receipt.read_text())
        row.update({key: timing[key] for key in ('started_ns', 'ended_ns') if key in timing})
        row['receipt'] = {'path': str(receipt), 'sha256': files['driver/receipt.json'],
                          'read': {'tool': 'pipeline_read', 'arguments': {'run': run, 'path': 'driver/receipt.json'}}}
    return row, original


def evaluate(pipeline, action):
    outer_id = action['id']; identity = run_id(outer_id)
    output = pipeline.root / identity
    output.mkdir(exist_ok=False)
    args = action.get('arguments', {})
    result = {'evaluation_status': 'failed', 'source_changed': False, 'source_sha256': None,
              'check_receipt': None, 'stages': [], 'feedback': None, 'continuation': None}
    status = 'completed'; deadline = None; prior_intent = None; requested_source = None

    def reload():
        return Pipeline(pipeline.config)

    def pick(kind, optional=False):
        explicit = args.get(kind + '_driver')
        if explicit is None and prior_intent: explicit = prior_intent['drivers'].get(kind)
        choices = [name for name, driver in pipeline.config['drivers'].items() if driver['kind'] == kind]
        if explicit is not None:
            if explicit not in choices: raise ValueError('Configured ' + kind + ' driver required')
            return explicit
        if not choices and optional: return None
        if len(choices) != 1: raise ValueError('Select exactly one ' + kind + '_driver from the configured drivers')
        return choices[0]

    def phase(stage, tool, arguments):
        number = len(result['stages']) + 1
        internal = outer_id + '/phase/' + str(number) + '-' + stage
        child = reload()
        save(output / ('phase-%04d-intent.json' % number), {'action_id': internal, 'run': run_id(internal),
            'stage': stage, 'tool': tool, 'arguments': arguments, 'origin_action_id': outer_id})
        child.apply({'id': internal, 'tool': tool, 'arguments': arguments},
                    acceptance_action_id=outer_id, execution_deadline=deadline)
        child = reload()
        record = next(row for row in child.state['receipts'] if row['run'] == run_id(internal))
        descriptor, original = phase_record(child, record['run'], stage, internal, record['status'])
        save(output / ('stage-%04d.json' % number), descriptor)
        result['stages'].append(descriptor)
        body = original['data']
        if isinstance(body, dict) and isinstance(body.get('result'), dict) and body['result'].get('evaluation'):
            result['feedback'] = body['result']['evaluation']
        if original.get('diagnostics') is not None:
            result['diagnostics'] = original['diagnostics']
        return child, original

    def pending(reason, state, target=None, *, ready=False):
        result.update(evaluation_status='pending', pending_kind='phase_ready' if ready else 'original_receipt', error=reason)
        handles = state.pending_launches()
        retained = state.matching_handle() if state.state.get('candidate') else None
        expected_source = requested_source or result.get('source_sha256') or (prior_intent or {}).get('binding', {}).get('source')
        if retained and expected_source and retained['source']['sha256'] != expected_source:
            retained = None
        result['original_handle'] = retained['handle'] if retained else None
        observed = next((row['observation'] for row in handles if row.get('observation')
                         and (expected_source is None or row['binding']['source'] == expected_source)), None)
        if retained is None and observed is not None:
            result.update(original_handle=observed['original_handle'], pending_observation=observed, observation_only=True)
        result['recover_run'] = handles[0]['run'] if handles else None
        if target or (output / 'evaluation-intent.json').exists():
            result['continuation'] = {'tool': 'pipeline_evaluate', 'arguments': {
                'resume_run': target or identity, 'wait_seconds': args.get('wait_seconds', 120)}}
        else:
            collectors = [name for name, driver in state.config['drivers'].items() if driver['kind'] == 'collect']
            result['continuation'] = ({'tool': 'pipeline_run', 'arguments': {'stage': 'E', 'driver': collectors[0],
                **({'recover_run': handles[0]['run']} if handles else {'handle': retained['handle']})}}
                if len(collectors) == 1 and (handles or retained) else None)
        result['recovery_scope'] = 'Resume this settled bounded call only. A crashed outer Journal action must first be reconciled by its original owner; this tool cannot bypass that fence.'

    def refused(original, state, launch_run):
        if original['data'].get('dispatch_outcome') != 'not_started': return False
        refusal = state.committed_launch_rejection(launch_run)
        if refusal is None:
            raise ValueError('Launch refusal lacks its committed original receipt')
        result.update(evaluation_status='failed', failure_phase=refusal['failure_phase'], dispatch_outcome='not_started',
            feedback=refusal, error=refusal['error']['message'], original_handle=None, continuation=None,
            qualification='No native child started; no F/G/H or device acceptance was produced.')
        return True

    try:
        allowed = {'files', 'origin', 'launch_driver', 'collect_driver', 'check_driver', 'wait_seconds', 'resume_run'}
        if not isinstance(args, dict) or set(args) - allowed: raise ValueError('Unknown evaluate arguments')
        seconds = args.get('wait_seconds', 120)
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 1800:
            raise ValueError('wait_seconds must be between 0 and 1800')
        deadline = time.monotonic() + seconds
        if not pipeline.state.get('contract') or not pipeline.state.get('C'):
            raise ValueError('Bind A and record the C implementation hypothesis before evaluation')
        resume = args.get('resume_run')
        prior_intent = None
        if resume is not None:
            if not isinstance(resume, str) or not re.fullmatch('[a-f0-9]{64}', resume): raise ValueError('Invalid resume_run')
            if 'files' in args or 'origin' in args: raise ValueError('Resume the original frozen source; do not supply replacement files')
            prior_intent = json.loads((pipeline.root / resume / 'evaluation-intent.json').read_text())
            if run_id(prior_intent['action_id']) != resume: raise ValueError('Original evaluation identity changed')
            current = pipeline.binding()
            if any(current[key] != prior_intent['binding'][key] for key in ('source', 'contract', 'baseline', 'revision')):
                raise ValueError('Resume must use the original candidate, contract and baseline binding')
            result['resume_run'] = resume
        pending_phases = pipeline.pending_phases() + pipeline.pending_effects()
        if pending_phases:
            result.update(evaluation_status='pending', error='Original phase may have executed without a committed receipt; inspect its original intent/logs, never execute a replacement',
                          unresolved_phases=pending_phases, continuation=None)
            return pipeline.finish(action, output, 'completed', result)
        pending_diagnostics = pipeline.pending_diagnostics()
        if pending_diagnostics:
            result.update(evaluation_status='pending', error='Original diagnostic execution is unresolved; inspect its existing intent/logs before any new device work',
                          unresolved_diagnostics=pending_diagnostics, continuation=None)
            return pipeline.finish(action, output, 'completed', result)
        last = pipeline.state.get('last_attempt') or {}
        if last.get('evaluation_outcome') == 'unknown' and last.get('stage') in {'F', 'G', 'J'}:
            result.update(evaluation_status='pending', error='Original evaluation is unresolved; inspect its original receipt/logs before any replacement execution',
                          unresolved_run=last['run'], continuation=None)
            return pipeline.finish(action, output, 'completed', result)
        before = (pipeline.state.get('candidate') or {}).get('sha256')
        unresolved = pipeline.pending_launches()
        if 'files' in args:
            if not isinstance(args.get('origin'), dict): raise ValueError('Explicit source origin is required')
            files = args['files']
            if not isinstance(files, dict) or not files or len(files) > 128: raise ValueError('Explicit source file mapping required')
            requested = digest({name: sha(pipeline.path(path)) for name, path in files.items()})
            requested_source = requested
            if unresolved and requested != before: raise ValueError('Recover the original launch before replacing its source')
            if requested != before:
                pipeline, frozen = phase('D', 'pipeline_freeze', {'files': files, 'origin': args['origin']})
                if frozen['status'] != 'completed': raise ValueError(frozen['data'].get('error', 'Freeze failed'))
                if pipeline.current()['sha256'] != requested: raise ValueError('Candidate changed while being frozen; no launch performed')
            else:
                pipeline.verify(pipeline.current())
        source = pipeline.current()
        result.update(source_sha256=source['sha256'], previous_source_sha256=before, source_changed=before != source['sha256'])
        drivers = {'launch': pick('launch', optional=True), 'collect': pick('collect', optional=True), 'check': pick('check')}
        if prior_intent and drivers != prior_intent['drivers']:
            raise ValueError('Resume must retain the original configured driver selection')
        intent = {'action_id': outer_id, 'binding': pipeline.binding(), 'drivers': drivers,
                  'source': source, 'resume_run': resume, 'wait_seconds': seconds}
        save(output / 'evaluation-intent.json', intent)

        last = pipeline.state.get('last_attempt') or {}
        if last.get('evaluation_outcome') == 'unknown' and last.get('stage') in {'F', 'G', 'J'}:
            result.update(evaluation_status='pending', error='Original evaluation is unresolved; inspect its original receipt/logs before any replacement execution',
                          unresolved_run=last['run'], continuation=None)
        else:
            retained = pipeline.matching_handle(for_retry=True)
            unknown = pipeline.pending_launches()
            if unknown and any(row['binding']['source'] != source['sha256'] or row['binding']['contract'] != pipeline.binding()['contract'] for row in unknown):
                raise ValueError('An unresolved launch for a different source/contract must be recovered separately')
            if not unknown and not retained and drivers['launch'] and time.monotonic() < deadline:
                pipeline, launched = phase('E', 'pipeline_run', {'stage': 'E', 'driver': drivers['launch']})
                if refused(launched, pipeline, result['stages'][-1]['run']):
                    return pipeline.finish(action, output, 'completed', result)
                retained = pipeline.matching_handle(); unknown = pipeline.pending_launches()
            polls = 0
            while (unknown or retained and not retained.get('collected')) and time.monotonic() < deadline and polls < 16:
                if not drivers['collect']: break
                collection = {'recover_run': unknown[0]['run']} if unknown else {'handle': retained['handle']}
                pipeline, collected = phase('E', 'pipeline_run', {'stage': 'E', 'driver': drivers['collect'], **collection})
                if collected['data'].get('dispatch_outcome') == 'not_started':
                    original_run = collection.get('recover_run')
                    if original_run is None or not refused(collected, pipeline, original_run):
                        raise ValueError('Native admission collection must identify its original launch')
                    return pipeline.finish(action, output, 'completed', result)
                polls += 1; retained = pipeline.matching_handle(); unknown = pipeline.pending_launches()
                if collected['status'] != 'completed': break
                if unknown or retained and not retained.get('collected'):
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            if unknown or retained and not retained.get('collected'):
                pending('Original job has not produced a terminal receipt within this bounded call; recover its existing identity', pipeline)
            elif drivers['launch'] and retained is None:
                pending('Bounded wait ended before launch; frozen source is ready for its first execution', pipeline, ready=True)
            elif time.monotonic() >= deadline and not (pipeline.state.get('check')
                    and pipeline.state['check']['binding'] == pipeline.binding()):
                pending('Bounded wait ended; original terminal job and frozen source are retained for F', pipeline, ready=True)
            else:
                existing = pipeline.state.get('check')
                if existing and existing['binding'] == pipeline.binding():
                    # Read an already committed F, including an orphan phase whose
                    # outer call crashed. Never execute a replacement check.
                    receipt_path = Path(existing['receipt']); folder = receipt_path.parent.parent
                    request = json.loads((folder / 'driver/request.json').read_text())
                    descriptor, original = phase_record(pipeline, folder.name, 'F', request['action_id'], 'completed')
                    result['check_receipt'] = original['data']; result['check_action_id'] = existing['action_id']
                    if original.get('diagnostics') is not None: result['diagnostics'] = original['diagnostics']
                    owner_run = run_id(existing['action_id'])
                    owner_committed = any(row['run'] == owner_run for row in pipeline.state['receipts'])
                    if not owner_committed and existing['action_id'] != outer_id:
                        if resume != owner_run:
                            pending('Resume the original owner of the committed F receipt; do not rerun F', pipeline, owner_run, ready=True)
                        else:
                            descriptor.update(origin_action_id=existing['action_id'], reused=True)
                            result['stages'].append(descriptor)
                            save(output / 'recovered-F.json', descriptor)
                            pipeline.state['check']['action_id'] = outer_id
                            result['check_action_id'] = outer_id
                    else:
                        result['reused_check'] = descriptor
                else:
                    check_args = {'stage': 'F', 'driver': drivers['check']}
                    if retained: check_args['handle'] = retained['handle']
                    pipeline, checked = phase('F', 'pipeline_run', check_args)
                    if checked['status'] == 'completed':
                        result['check_receipt'] = checked['data']; result['check_action_id'] = outer_id
                    else:
                        result['error'] = checked['data'].get('error', 'Original F did not settle')
                        if checked['data'].get('evaluation_outcome') == 'unknown':
                            result.update(evaluation_status='pending', continuation=None,
                                          unresolved_run=result['stages'][-1]['run'])
                if result['evaluation_status'] != 'pending':
                    check = pipeline.state.get('check') or {}
                    result['evaluation_status'] = 'completed' if check.get('passed') else 'failed'
                if result['feedback'] is None and retained:
                    result['feedback'] = retained.get('evaluation')
    except Exception as error:
        # Child commits and launch intents survive errors in the composite wrapper.
        pipeline = reload(); result['error'] = str(error); status = 'failed'
        if pipeline.pending_launches():
            pending(str(error), pipeline); status = 'completed'
    if result.get('feedback') is None and isinstance(result.get('check_receipt'), dict):
        original_error = result['check_receipt'].get('result', {}).get('error')
        if isinstance(original_error, str) and original_error:
            phase = next((row for row in result['stages'] if row['stage'] == 'F'), result.get('reused_check', {}))
            result['feedback'] = {'error_excerpt': original_error[:2048], 'truncated': len(original_error) > 2048,
                                  'original_receipt': phase.get('receipt')}
    result['qualification'] = 'F correctness only. G, H and host acceptance still require their original source-bound receipts.'
    return pipeline.finish(action, output, status, result, result.pop('diagnostics', None))
