"""Receipt-derived information progress, never authority to settle or retry effects.

Pinned tools may annotate an outcome's data with continuation_progress/v1:
{version: 1, identity, fingerprint, state: 'progress'|'waiting', summary?,
 receipt_refs?: [{tool, arguments}]}. Stable identities/fingerprints describe
the original resource and semantic observation, excluding transport envelopes.
"""
import copy
import re

from kernelcat_executor.contracts.models import canonical, digest

BOOKKEEPING_TOOLS = frozenset({'get_goal', 'update_goal', 'update_plan'})


def progress_signal(value):
    """Return a detached bounded signal, or None for malformed/unrecognized data."""
    if (not isinstance(value, dict) or set(value) - {'version', 'identity', 'fingerprint', 'state', 'summary', 'receipt_refs'}
            or type(value.get('version')) is not int or value['version'] != 1
            or not isinstance(value.get('state'), str) or value['state'] not in {'progress', 'waiting'}):
        return None
    identity = value.get('identity')
    fingerprint = value.get('fingerprint')
    if (not isinstance(identity, str) or not identity.strip() or len(identity.encode()) > 2048
            or not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint)):
        return None
    summary = value.get('summary', '')
    if not isinstance(summary, str) or len(summary.encode()) > 4096:
        return None
    refs = value.get('receipt_refs', [])
    if not isinstance(refs, list) or len(refs) > 4:
        return None
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {'tool', 'arguments'}:
            return None
        if (not isinstance(ref['tool'], str) or not ref['tool'].strip() or len(ref['tool'].encode()) > 128
                or not isinstance(ref['arguments'], dict)):
            return None
        try:
            if len(canonical(ref['arguments']).encode()) > 4096:
                return None
        except (TypeError, ValueError):
            return None
    return copy.deepcopy(value)


def observation_fingerprint(action, outcome):
    """Fallback for ordinary tools; domain wrappers should supply a stable signal.

Only known execution envelope fields and the exact host-added native receipt
are omitted. Error/output bytes themselves remain in the fingerprint.
"""
    data = copy.deepcopy(outcome.get('data', {}))
    data.pop('execution', None)
    data.pop('elapsed_ms', None)
    if isinstance(data.get('adapter'), str) and data['adapter'].startswith('pi@') and data.get('native_declaration_digest'):
        result = data.get('result')
        if isinstance(result, dict) and isinstance(result.get('content'), list):
            marker = {'type': 'text', 'text': 'Execution receipt: ' + action['id']}
            if result['content'] and result['content'][-1] == marker:
                result['content'].pop()
    return digest({'status': outcome.get('status'), 'data': data})


def continuation_activity(events, receipt, *, task_id=None, goal_id=None, start_seq=-1):
    """Inspect one completed foreground input and already-observed information.

Admission alone is not progress. A new compiler failure or newly read evidence
can be information progress; a repeated identical failure/page/status cannot.
Background tasks, later inputs and observations after completion are excluded.
Unknown effects remain unknown and are never promoted to completed progress.
"""
    begin = receipt.get('applied_seq', receipt['accepted_seq'])
    end = receipt.get('completed_seq')
    if not isinstance(end, int):
        return {'progress': False, 'waiting': False, 'receipt_refs': [], 'new_action_ids': [], 'has_tool_actions': False}
    admitted = {}
    seen = set()
    new_ids = []
    refs = []
    waiting = {}
    unknown = False
    invalid = False
    has_tool_actions = False
    input_id = receipt.get('input_id')
    for row in events:
        seq = row.get('seq', -1)
        if not start_seq < seq <= end:
            continue
        if row['kind'] == 'admitted':
            action = row['action']
            context = row.get('application_context', {})
            if (action['tool'].startswith('__') or action['tool'] in BOOKKEEPING_TOOLS
                    or (task_id is not None and action.get('task') != task_id)
                    or (goal_id is not None and context.get('goal_id') not in (None, goal_id))):
                continue
            current = seq > begin and (input_id is None or context.get('input_id') == input_id)
            # A future/different input must not influence either this input's
            # progress or its prior-information set.
            if seq > begin and not current:
                continue
            admitted[action['id']] = (action, current)
            if current:
                has_tool_actions = True
        elif row['kind'] == 'settled' and row.get('action_id') in admitted:
            action, current = admitted[row['action_id']]
            # Old foreground work which settles during this input was not
            # observed at its start, and is not this input's progress either.
            if not current and seq > begin:
                continue
            outcome = row['outcome']
            data = outcome.get('data', {})
            if outcome.get('status') == 'unknown':
                if current:
                    unknown = True
                    refs.append({'tool': 'read_receipt', 'arguments': {'action_id': action['id']}})
                continue
            if outcome.get('status') not in {'completed', 'failed'}:
                continue
            has_signal = 'continuation_progress' in data
            signal = progress_signal(data['continuation_progress']) if has_signal else None
            if has_signal and signal is None:
                if current:
                    invalid = True
                    refs.append({'tool': 'read_receipt', 'arguments': {'action_id': action['id']}})
                # A malformed claimed semantic signal cannot fall back to
                # fresh wrapper IDs and fabricate progress.
                continue
            if signal:
                key = (signal['identity'], signal['fingerprint'])
            else:
                key = (digest({'tool': action['tool'], 'arguments': action.get('arguments', {})}),
                       observation_fingerprint(action, outcome))
            if current:
                if key not in seen:
                    new_ids.append(action['id'])
                if signal:
                    if signal['state'] == 'waiting':
                        waiting[signal['identity']] = {**signal, 'receipt_refs': signal.get('receipt_refs') or [
                            {'tool': 'read_receipt', 'arguments': {'action_id': action['id']}}]}
                    elif (signal['identity'] in waiting
                          and signal['fingerprint'] != waiting[signal['identity']]['fingerprint']):
                        # Only a producer's newer observation of the same
                        # original resource can resolve its waiting signal.
                        waiting.pop(signal['identity'], None)
                candidates = (signal or {}).get('receipt_refs') or [
                    {'tool': 'read_receipt', 'arguments': {'action_id': action['id']}}]
                for reference in candidates:
                    if reference not in refs:
                        refs.append(reference)
            seen.add(key)
    waiting_refs = [ref for signal in waiting.values() for ref in signal.get('receipt_refs', [])]
    return {'progress': bool(new_ids), 'waiting': bool(waiting) or unknown,
            'receipt_refs': (waiting_refs or refs)[-4:],
            'new_action_ids': new_ids, 'invalid_signal': invalid, 'has_tool_actions': has_tool_actions}
