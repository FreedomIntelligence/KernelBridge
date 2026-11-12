"""Export completed NPU campaigns without credentials or transport configuration."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(campaign, output):
    config = json.loads((campaign / 'trial-config.json').read_text())
    mode = config['mode']
    folder = output / mode
    folder.mkdir()
    rows = []
    for number in range(1, 21):
        original = campaign / 'trials' / f'round-{number:02d}'
        result = json.loads((original / 'result.json').read_text())
        source = original / 'candidate.py'
        device = original / 'device-result.json'
        if result['round'] != number or digest(source) != result['source_sha256']:
            raise ValueError(f'{mode} round {number}: source identity mismatch')
        if not result.get('original_handle') or not result.get('remote_result_sha256'):
            raise ValueError(f'{mode} round {number}: original device receipt missing')
        # The original remote receipt and local qualification record remain in
        # the campaign directory. Both identities are retained; transport stays private.
        raw = json.loads(device.read_text())
        retained = folder / f'round-{number:02d}.py'
        shutil.copyfile(source, retained)
        rows.append({key: result.get(key) for key in
                     ('round', 'operator', 'status', 'correct', 'speedup', 'hypothesis', 'source_sha256', 'cases')} | {
            'source': f'{mode}/{retained.name}', 'device_receipt_sha256': digest(device),
            'remote_result_sha256': result['remote_result_sha256'],
            'environment': raw.get('environment'), 'baseline': raw.get('baseline'),
            'baseline_sha256': raw.get('baseline_sha256'), 'driver_sha256': raw.get('driver_sha256'),
            'failure_kind': raw.get('error'),
            'diagnostics_trigger': (raw.get('diagnostics') or {}).get('reason'),
            'diagnostics': {evidence['kind']: {'status': evidence.get('status'), 'reason': evidence.get('reason')}
                            for evidence in (raw.get('diagnostics') or {}).get('reports', [])},
        })
    correct = [row for row in rows if row['correct'] and isinstance(row['speedup'], (int, float))]
    best = max(correct, key=lambda row: row['speedup']) if correct else None
    if best:
        shutil.copyfile(output / best['source'], folder / 'best-observed.py')
    shutil.copyfile(campaign / 'project/contract.json', folder / 'contract.json')
    return {'operator': mode, 'rounds': len(rows), 'correct_rounds': len(correct),
            'best_observed': None if best is None else {key: best[key] for key in ('round', 'speedup', 'source_sha256', 'source', 'cases')},
            'results': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = [summarize(campaign.resolve(), args.output) for campaign in args.campaign]
    summary = {'scope': 'same-device NPU native candidate versus Torch NPU eager composition',
               'qualification': 'Local declared suite only. Cross-round best-observed ranking is not paired incumbent promotion or an official score.',
               'campaigns': results}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    with (args.output / 'rounds.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['operator', 'round', 'correct', 'speedup', 'source_sha256', 'hypothesis'])
        writer.writeheader()
        for campaign in results:
            for row in campaign['results']:
                writer.writerow({key: row[key] for key in writer.fieldnames})
    print(json.dumps({entry['operator']: {'rounds': entry['rounds'], 'correct': entry['correct_rounds'],
        'best_speedup': (entry['best_observed'] or {}).get('speedup')} for entry in results}))


if __name__ == '__main__':
    main()
