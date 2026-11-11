"""Read-only accounting of a fresh operator trial from its original Journal.

This report observes calls and accepted evidence. It neither executes tools nor
turns model statements or screen text into correctness/performance evidence.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def read_events(path):
    rows = []
    previous = '0' * 64
    with path.open() as stream:
        for seq, line in enumerate(stream):
            if not line.endswith('\n'):
                raise ValueError('Journal has an incomplete tail; wait for the original writer')
            row = json.loads(line)
            claimed = row.pop('digest')
            actual = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':'),
                ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            if row.get('prev') != previous or claimed != actual or row.get('seq') != seq:
                raise ValueError('Journal hash chain or sequence mismatch')
            previous = claimed
            if row['kind'] == 'atomic':
                rows.extend(dict(item, seq=seq) for item in row['events'])
            else:
                rows.append(row)
    return rows, previous


def role(action):
    if '/compact/' in action['id']:
        return 'compact'
    if '/goal-review/' in action['id']:
        return 'review'
    return 'child' if action['task'].endswith('/leaf') else 'parent'


def tokens(usage):
    def number(name):
        value = usage.get(name)
        return value if type(value) is int and value >= 0 else 0
    if type(usage.get('total_tokens')) is int:
        return number('total_tokens')
    if 'prompt_tokens' in usage:
        return number('prompt_tokens') + number('completion_tokens')
    return sum(number(key) for key in ('input_tokens', 'output_tokens',
        'cache_read_input_tokens', 'cache_creation_input_tokens'))


def audit(run):
    journal = run / 'state/events.jsonl'
    events, head = read_events(journal)
    admitted = {row['action']['id']: row for row in events if row['kind'] == 'admitted'}
    settled = {row['action_id']: row for row in events if row['kind'] == 'settled'}
    models = [row for row in admitted.values() if row['action']['tool'] == '__propose__']
    groups = {name: dict(calls=0, tokens=0, usage_unknown_calls=0, pending_calls=0)
              for name in ('parent', 'compact', 'child', 'review')}
    model_rows = []
    for index, row in enumerate(models):
        action = row['action']; category = role(action); group = groups[category]
        end = settled.get(action['id']); outcome = (end or {}).get('outcome', {})
        usage = (outcome.get('data', {}).get('usage')
                 or outcome.get('details', {}).get('usage')
                 or outcome.get('data', {}).get('failure_details', {}).get('usage'))
        group['calls'] += 1
        group['pending_calls'] += int(end is None)
        if isinstance(usage, dict) and usage:
            group['tokens'] += tokens(usage)
        elif end is not None:
            group['usage_unknown_calls'] += 1
        content = outcome.get('data', {}).get('content', [])
        names = [block.get('name') for block in content if block.get('type') in ('tool_use', 'toolCall', 'tool_call')]
        model_rows.append({'turn': index + 1, 'action_id': action['id'], 'role': category,
            'admitted_seq': row['seq'], 'status': outcome.get('status', 'pending'),
            'tokens': tokens(usage) if isinstance(usage, dict) and usage else None,
            'elapsed_ms': (end or {}).get('elapsed_ms'), 'proposed_tools': names})
    calls = []; stages = Counter(); diagnostics = []; reads = []
    for identity, row in admitted.items():
        action = row['action']; tool = action['tool']; args = action['arguments']
        if tool.startswith('__'):
            continue
        end = settled.get(identity); outcome = (end or {}).get('outcome', {})
        data = outcome.get('data', {}); result = data.get('result')
        item = {'action_id': identity, 'tool': tool, 'role': role(action), 'admitted_seq': row['seq'],
            'status': outcome.get('status', 'pending'), 'elapsed_ms': (end or {}).get('elapsed_ms'),
            'run': data.get('run'), 'initiator': 'host_seed' if '/seed/' in identity else 'model'}
        stage = args.get('stage') or {'pipeline_bind': 'A', 'pipeline_freeze': 'D',
            'pipeline_pair': 'G', 'pipeline_promote': 'H', 'pipeline_archive': 'I'}.get(tool)
        if stage:
            item['stage'] = stage; stages[stage] += 1
        if args.get('driver'):
            item['driver'] = args['driver']
        if isinstance(result, dict):
            if result.get('error'):
                item['error'] = result['error']
            if result.get('diagnostics'):
                item['automatic_diagnostics'] = result['diagnostics']
            if tool == 'pipeline_pair':
                item['pair'] = {key: result.get(key) for key in
                    ('speedup', 'cases', 'families', 'regressions', 'qualification')}
        if stage == 'J':
            diagnostics.append(item)
        if tool == 'pipeline_read':
            reads.append({'action_id': identity, 'run': args.get('run'), 'path': args.get('path'),
                'offset': args.get('offset', 0), 'status': outcome.get('status', 'pending')})
        calls.append(item)
    goals = [row['data']['goal'] if row['kind'] == 'application/created' else row['data']
             for row in events if row['kind'] == 'application/goal'
             or row['kind'] == 'application/created' and row['data'].get('goal')]
    evidence = [row['data'] for row in events if row['kind'] == 'application/evidence']
    promoted = [row for row in evidence if row.get('evidence_tier') == 'E3']
    return {'schema_version': 'kernelbridge.operator-trial-audit/v1', 'run': str(run),
        'journal': str(journal), 'journal_sha256': hashlib.sha256(journal.read_bytes()).hexdigest(),
        'journal_head': head, 'journal_verified': True,
        'model_turns': len(models), 'accounting': 'all_model_calls_including_compact_child_review',
        'by_role': groups, 'tokens': sum(group['tokens'] for group in groups.values()),
        'usage_unknown_calls': sum(group['usage_unknown_calls'] for group in groups.values()),
        'within_20_calls': len(models) <= 20, 'goal': goals[-1] if goals else None,
        'evidence': evidence, 'accepted_e3': bool(promoted), 'e4_claimed': False,
        'stages_called': dict(stages),
        'tool_counts': dict(Counter(item['tool'] for item in calls if item['initiator'] == 'model')),
        'initialization_tool_counts': dict(Counter(item['tool'] for item in calls if item['initiator'] == 'host_seed')),
        'explicit_diagnostic_calls': diagnostics, 'artifact_reads': reads,
        'model_calls': model_rows, 'tool_calls': calls,
        'pending_action_ids': [identity for identity in admitted if identity not in settled],
        'observation_boundary': 'J calls and artifact reads are observed separately. A tool being available or automatic capture does not prove the model read or used its diagnostics.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.run.resolve(strict=True))
    encoded = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(encoded)
        print(json.dumps({key: result[key] for key in ('run', 'model_turns', 'tokens', 'by_role', 'goal', 'accepted_e3', 'stages_called')}))
    else:
        print(encoded, end='')


if __name__ == '__main__':
    main()
