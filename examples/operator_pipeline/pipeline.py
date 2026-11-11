"""Source-bound A-J workflow. Drivers execute; the harness chooses the next step.

The enclosing executor Journal owns effect admission and budgets. This local
receipt chain only projects operator state, never authorizes model execution.
"""
import fcntl
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import zipfile


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open('xb') as stream:
        stream.write(encoded(value) + b'\n'); stream.flush(); os.fsync(stream.fileno())


class Pipeline:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['state']).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = {'revision': 0, 'stage': None, 'contract': None, 'baseline': None, 'incumbent': None,
                      'candidate': None, 'check': None, 'pair': None, 'handles': [], 'receipts': []}
        self.chain = '0' * 64
        for path in sorted(self.root.glob('event-*.json')):
            row = json.loads(path.read_text())
            if row['parent'] != self.chain or row['sha256'] != digest(row['body']):
                raise ValueError('Operator receipt chain changed')
            self.state = row['body']; self.chain = sha(path)

    def path(self, value):
        path = Path(value).resolve(strict=True)
        if not any(path.is_relative_to(Path(root)) for root in [*self.config['read_roots'], str(self.root)]):
            raise ValueError('Source outside configured read roots')
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Source must be a bounded regular file')
        return path

    def freeze(self, files, folder):
        if not isinstance(files, dict) or not files or len(files) > 128:
            raise ValueError('Explicit nonempty file mapping required')
        folder.mkdir()
        manifest = {}
        for name, value in files.items():
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or name in ('', '.'):
                raise ValueError('Invalid frozen relative path')
            data = self.path(value).read_bytes()
            target = folder / relative; target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream: stream.write(data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        return {'root': str(folder), 'files': manifest, 'sha256': digest(manifest)}

    def verify(self, source):
        if not source or digest(source['files']) != source['sha256']:
            raise ValueError('Missing or changed frozen source')
        for name, expected in source['files'].items():
            path = Path(source['root']) / name
            if path.resolve() != path or not path.is_file() or sha(path) != expected:
                raise ValueError('Frozen source changed: ' + name)

    def current(self):
        s = self.state
        if not s['contract'] or not s['candidate']:
            raise ValueError('Bind contract and freeze candidate first')
        self.verify(s['candidate']); return s['candidate']

    def binding(self):
        return {'revision': self.state['revision'], 'contract': digest(self.state['contract']),
                'source': self.current()['sha256'], 'baseline': (self.state['baseline'] or {}).get('sha256')}

    def pending_launches(self):
        return [{'run': path.parent.name, **json.loads(path.read_text())}
                for path in sorted(self.root.glob('*/launch-intent.json'))
                if not any(item.get('launch_run') == path.parent.name for item in self.state['handles'])]

    def driver(self, name, source, folder, role, handle=None, *, contract=None, binding=None, recovery=None):
        driver = self.config['drivers'].get(name)
        if not driver: raise ValueError('Driver is not configured: ' + name)
        self.verify(source)
        contract = self.state['contract'] if contract is None else contract
        self.verify(contract['oracle']['files'])
        for path, expected in driver['files'].items():
            if sha(path) != expected: raise ValueError('Configured driver changed')
        folder.mkdir()
        request = {'source': source, 'role': role, 'contract': contract, 'handle': handle,
                   'binding': self.binding() if binding is None else binding, 'output': str(folder)}
        if recovery is not None: request['recovery'] = recovery
        save(folder / 'request.json', request)
        if driver['kind'] == 'launch':
            if self.pending_launches():
                raise ValueError('Recover the original unresolved launch before starting another task')
            # This intent survives a timeout, parser error, or killed adapter,
            # even before the ordinary receipt chain can commit the result.
            save(folder.parent / 'launch-intent.json', {'driver': name, 'driver_files': driver['files'],
                'source': source, 'contract': contract, 'binding': request['binding'], 'output': str(folder)})
        # No shell interpolation, credentials or implicit gateway. Explicit drivers
        # receive one JSON request and return one JSON result on stdout.
        started = time.time_ns()
        with (folder / 'stdout.log').open('wb') as stdout, (folder / 'stderr.log').open('wb') as stderr:
            result = subprocess.run(driver['argv'], input=encoded(request), stdout=stdout, stderr=stderr,
                cwd=source['root'], env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONDONTWRITEBYTECODE': '1'},
                timeout=driver.get('timeout', 600), check=False)
        self.verify(source)
        if result.returncode: raise ValueError('Driver failed; inspect original logs')
        raw = (folder / 'stdout.log').read_bytes()
        if len(raw) > 4 * 1024 * 1024: raise ValueError('Driver result too large')
        data = json.loads(raw)
        receipt = {'driver': name, 'kind': driver['kind'], 'driver_files': driver['files'],
                   'started_ns': str(started), 'ended_ns': str(time.time_ns()), 'source': source['sha256'],
                   'contract': digest(contract), 'result': data}
        save(folder / 'receipt.json', receipt)
        return receipt

    def check_passed(self):
        check = self.state['check']
        if not check or check['binding'] != self.binding() or not check['passed']:
            raise ValueError('Current source needs a complete passing correctness receipt')

    def execute(self, tool, args, output):
        s = self.state
        if tool == 'pipeline_status':
            return dict(s, drivers={key: value['kind'] for key, value in self.config['drivers'].items()},
                        pending_launches=self.pending_launches(),
                        transitions='A→B→C→D→E→F→G→H→I→B; F/G→J→C', device_acceptance='only as declared by original driver receipts')
        if tool == 'pipeline_bind':
            contract = args['contract']
            if not all(contract.get(k) for k in ['task', 'oracle', 'measurement', 'suite']):
                raise ValueError('task, oracle, measurement and suite are required')
            suite = contract['suite']
            if not isinstance(suite, list) or not suite or any(not isinstance(case, dict) or not case.get('id') for case in suite) or len({case['id'] for case in suite}) != len(suite):
                raise ValueError('Suite needs unique nonempty case IDs')
            oracle = contract['oracle']
            if oracle.get('kind') not in {'reference', 'independent', 'properties', 'official'} or not oracle.get('description'):
                raise ValueError('An independent correctness oracle is required, even without Torch or a baseline')
            if not oracle.get('files'): raise ValueError('Pin the oracle/checker source files')
            frozen_oracle = self.freeze(oracle['files'], output / 'oracle')
            contract = dict(contract, oracle=dict(oracle, files=frozen_oracle))
            s.update(contract=contract, baseline=None, candidate=None, check=None, pair=None, incumbent=None,
                     revision=s['revision'] + 1, stage='A')
            for key in ('B', 'C', 'diagnostics'): s.pop(key, None)
            return {'contract_sha256': digest(contract), 'baseline': None, 'oracle': frozen_oracle['sha256']}
        if not s['contract']: raise ValueError('Call pipeline_bind first')
        if tool == 'pipeline_plan':
            if not args['record']: raise ValueError('Retain workload evidence or a falsifiable hypothesis')
            s['stage'] = args['stage']; s[args['stage']] = args['record']; return args['record']
        if tool == 'pipeline_freeze':
            if not s.get('C'): raise ValueError('Record the implementation hypothesis first')
            source = self.freeze(args['files'], output / 'source')
            s.update(candidate=source, check=None, pair=None, stage='D', origin=args['origin'])
            return source
        if tool == 'pipeline_baseline':
            selection = args['selection']
            baseline = s['incumbent'] if selection == 'incumbent' else None
            if selection == 'incumbent' and not baseline: raise ValueError('No accepted incumbent yet')
            if selection == 'files': baseline = self.freeze(args['files'], output / 'baseline')
            if baseline: self.verify(baseline)
            s.update(baseline=baseline, check=None, pair=None, revision=s['revision'] + 1)
            return {'baseline': baseline, 'comparisons_invalidated': True, 'oracle_unchanged': True}
        collection = None; recovery = None
        if tool == 'pipeline_run' and self.config['drivers'].get(args['driver'], {}).get('kind') == 'collect':
            if args.get('recover_run'):
                if args.get('handle'): raise ValueError('Recovery looks up the original handle; do not supply a replacement')
                recovery = next((item for item in self.pending_launches() if item['run'] == args['recover_run']), None)
                if recovery is None: raise ValueError('No unresolved launch matches recover_run')
                collection = recovery
            else:
                collection = next((item for item in s['handles'] if item.get('handle') == args.get('handle')), None)
                if collection is None: raise ValueError('Collect using an original retained handle')
        elif args.get('recover_run'):
            raise ValueError('Only a collect driver may recover an original launch')
        source = collection['source'] if collection else self.current()
        self.verify((collection['contract'] if collection else s['contract'])['oracle']['files'])
        if tool == 'pipeline_run':
            stage = args['stage']; driver = self.config['drivers'].get(args['driver'], {})
            allowed = {'E': {'launch', 'collect'}, 'F': {'check'}, 'I': {'calibrate'}, 'J': {'diagnostic'}}
            if driver.get('kind') not in allowed[stage]: raise ValueError('Driver kind does not match stage')
            if stage == 'I':
                self.check_passed()
                if s['incumbent'] != source: raise ValueError('Calibrate the accepted incumbent')
            handle = args.get('handle')
            receipt = self.driver(args['driver'], source, output / 'driver', stage, handle,
                **({'contract': collection['contract'], 'binding': collection['binding'], 'recovery': recovery} if collection else {}))
            data = receipt['result']
            if stage == 'E' and (driver['kind'] == 'launch' or recovery):
                if not isinstance(data.get('handle'), dict) or not data['handle']:
                    raise ValueError('Launch/recovery must retain the original task handle')
                if any(item['handle'] == data['handle'] for item in s['handles']):
                    raise ValueError('Launch reused an existing handle; recover the original launch instead')
                s['handles'].append({'handle': data['handle'], 'source': copy.deepcopy(source),
                    'contract': copy.deepcopy(collection['contract'] if collection else s['contract']),
                    'binding': collection['binding'] if collection else self.binding(),
                    'driver': recovery['driver'] if recovery else args['driver'],
                    'launch_run': recovery['run'] if recovery else output.name})
            if stage == 'F':
                checks = data.get('checks', {})
                passed = (all(checks.get(k) is True for k in ('build', 'semantics', 'full'))
                          and type(data.get('case_count')) is int and data['case_count'] > 0
                          and data['case_count'] == len(s['contract']['suite'])
                          and sorted(data.get('case_ids', [])) == sorted(case['id'] for case in s['contract']['suite'])
                          and data.get('oracle_sha256') == s['contract']['oracle']['files']['sha256'])
                s['check'] = {'binding': self.binding(), 'passed': passed, 'receipt': str(output / 'driver/receipt.json'),
                              'scope': data.get('scope', 'unspecified')}
                s['pair'] = None
            if stage == 'J': s['diagnostics'] = {'receipt': str(output / 'driver/receipt.json'), 'source': source['sha256']}
            s['stage'] = stage
            return receipt
        if tool == 'pipeline_pair':
            self.check_passed()
            baseline = s['baseline']
            if not baseline: return {'paired': False, 'improvement': None, 'reason': 'No performance baseline. Establish the first correct incumbent.'}
            if self.config['drivers'].get(args['driver'], {}).get('kind') != 'measure':
                raise ValueError('Pairing requires a measurement driver')
            arms = []
            for index, selected in enumerate([baseline, source, source, baseline]):
                arms.append(self.driver(args['driver'], selected, output / ('arm-' + str(index)), 'ABBA'[index]))
            results = [arm['result'] for arm in arms]
            ids = set(results[0].get('cases', {}))
            env = results[0].get('environment')
            if not ids or not env or any(set(r.get('cases', {})) != ids or r.get('environment') != env
                or r.get('measurement') != s['contract']['measurement'] or r.get('correct') is not True for r in results):
                raise ValueError('ABBA requires identical full cases, environment, measurement and correct arms')
            if ids != {case['id'] for case in s['contract']['suite']}:
                raise ValueError('ABBA omitted declared cases')
            medians = []
            for result in results:
                values = {}
                for case, samples in result['cases'].items():
                    if not isinstance(samples, list) or not samples or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in samples):
                        raise ValueError('Timing samples must be positive finite measurements')
                    values[case] = statistics.median(samples)
                medians.append(values)
            ratios = {case: (medians[0][case] + medians[3][case]) / (medians[1][case] + medians[2][case]) for case in sorted(ids)}
            overall = math.exp(sum(math.log(r) for r in ratios.values()) / len(ratios))
            families = {}
            for case in s['contract']['suite']:
                families.setdefault(case.get('family', 'all'), []).append(ratios[case['id']])
            s['pair'] = {'binding': self.binding(), 'speedup': overall, 'cases': ratios,
                         'regressions': [case for case, ratio in ratios.items() if ratio < 1],
                         'families': {name: math.exp(sum(map(math.log, values)) / len(values)) for name, values in families.items()},
                         'environment': env, 'receipts': [str(output / ('arm-' + str(i)) / 'receipt.json') for i in range(4)]}
            s['stage'] = 'G'; return s['pair']
        if tool == 'pipeline_promote':
            self.check_passed(); pair = s['pair']
            if s['baseline'] and (not pair or pair['binding'] != self.binding() or pair['speedup'] <= 1):
                raise ValueError('Promotion requires a positive complete paired result against the selected baseline')
            s.update(incumbent=source, stage='H')
            return {'incumbent': source, 'bootstrap': s['baseline'] is None, 'speedup': pair['speedup'] if pair else None,
                    'scope': s['check']['scope'], 'baseline_selection': 'Call pipeline_baseline(selection="incumbent") before the next iteration'}
        if tool == 'pipeline_archive':
            self.check_passed()
            if s['incumbent'] != source: raise ValueError('Archive only the accepted source')
            archive = output / 'source.zip'
            with zipfile.ZipFile(archive, 'x') as bundle:
                for name in sorted(source['files']): bundle.write(Path(source['root']) / name, name)
            record = {'source': source, 'archive_sha256': sha(archive), 'binding': self.binding(), 'check': s['check'],
                      'pair': s['pair'], 'knowledge': args['knowledge'], 'published': False}
            save(output / 'release.json', record); s['stage'] = 'I'; return record
        raise ValueError('Unknown pipeline tool')

    def apply(self, action):
        identity = hashlib.sha256(action['id'].encode()).hexdigest()
        output = self.root / identity
        # No implicit rerun after a lost reply. The outer Journal recovers the
        # original effect; an incomplete directory requires reconciliation.
        output.mkdir(exist_ok=False)
        previous = copy.deepcopy(self.state)
        try:
            result = self.execute(action['tool'], action['arguments'], output); status = 'completed'
        except Exception as error:
            self.state = previous
            result = {'error': str(error)}; status = 'failed'
            if (output / 'launch-intent.json').exists():
                result.update(launch_outcome='unknown', recover_run=identity,
                              recovery='Use a collect driver to recover this original launch; never redispatch')
        save(output / 'result.json', {'status': status, 'data': result})
        files = {str(p.relative_to(output)): sha(p) for p in output.rglob('*') if p.is_file() and not p.is_symlink()}
        save(output / 'manifest.json', files)
        self.state['receipts'].append({'run': identity, 'tool': action['tool'], 'status': status,
                                       'manifest_sha256': sha(output / 'manifest.json')})
        save(self.root / ('event-%08d.json' % len(self.state['receipts'])), {'parent': self.chain, 'body': self.state, 'sha256': digest(self.state)})
        return {'status': status, 'data': {'run': identity, 'result': result if len(encoded(result)) < 16000 else 'Read result.json with pipeline_read'}}


def main(config, action):
    root = Path(config['state']); root.mkdir(parents=True, exist_ok=True)
    with (root / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pipeline = Pipeline(config)
        if action['tool'] == 'pipeline_read':
            args = action['arguments']; identity = args['run']
            if not re.fullmatch('[a-f0-9]{64}', identity): raise ValueError('Invalid run')
            record = next(r for r in pipeline.state['receipts'] if r['run'] == identity)
            folder = root / identity; manifest = folder / 'manifest.json'
            if sha(manifest) != record['manifest_sha256']: raise ValueError('Artifact manifest changed')
            files = json.loads(manifest.read_text()); name = args['path']; path = folder / name
            if name not in files or path.resolve() != path or not path.is_relative_to(folder) or sha(path) != files[name]:
                raise ValueError('Artifact missing or changed')
            offset = args.get('offset', 0); limit = min(args.get('limit', 8192), 16384)
            with path.open('rb') as stream: stream.seek(offset); data = stream.read(limit)
            return {'status': 'completed', 'data': {'text': data.decode(errors='replace'), 'next_offset': offset + len(data)}}
        return pipeline.apply(action)


if __name__ == '__main__':
    try: result = main(json.loads(Path(sys.argv[1]).read_text()), json.load(sys.stdin))
    except Exception as error: result = {'status': 'failed', 'data': {'error': str(error)}}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
