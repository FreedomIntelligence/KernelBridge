"""Source-bound A-J workflow. Drivers execute; the harness chooses the next step.

The enclosing executor Journal owns effect admission and budgets. This local
receipt chain only projects operator state, never authorizes model execution.
"""
import base64
import fcntl
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile
from diagnostic_policy import diagnostic_reason, verify_record
from qualification import aggregate, qualify
from job_identity import same_handle, wire_handles
from preflight import PROTOCOL as PREFLIGHT_PROTOCOL, validate_rejection


class PreflightRejected(Exception):
    """A verified, pinned launch adapter proved its dispatch never started."""
    def __init__(self, record, feedback):
        super().__init__(feedback['error']['message'])
        self.record, self.feedback = record, feedback


def preflight_json(raw):
    """Typed refusal output cannot hide conflicting duplicate JSON fields."""
    def fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate preflight JSON field')
            value[key] = item
        return value
    def invalid_constant(_value):
        raise ValueError('Nonfinite preflight JSON value')
    return json.loads(raw, object_pairs_hook=fields, parse_constant=invalid_constant)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open('xb') as stream:
        stream.write(encoded(value) + b'\n'); stream.flush(); os.fsync(stream.fileno())


def diagnostic_policy(config):
    policy = config.get('diagnostics')
    if policy is None:
        return None
    if not isinstance(policy, dict) or set(policy) - {'on_failure', 'on_regression', 'required', 'drivers', 'scope'}:
        raise ValueError('Invalid diagnostics policy')
    for name in ('on_failure', 'on_regression', 'required'):
        if name in policy and type(policy[name]) is not bool:
            raise ValueError('Diagnostics switches must be boolean')
    drivers = policy.get('drivers', {})
    if not isinstance(drivers, dict) or set(drivers) - {'ir', 'profiler'}:
        raise ValueError('Diagnostics drivers must map ir/profiler to configured names')
    for name in drivers.values():
        if not isinstance(name, str) or config.get('drivers', {}).get(name, {}).get('kind') != 'diagnostic':
            raise ValueError('Diagnostics requires configured diagnostic drivers')
    scope = policy.get('scope')
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError('Diagnostics requires an explicit evidence scope, e.g. npu_diagnostic or local_cpu_diagnostic')
    return {'on_failure': policy.get('on_failure', True), 'on_regression': policy.get('on_regression', True),
            'required': policy.get('required', False), 'drivers': drivers, 'scope': scope}


class Pipeline:
    def __init__(self, config):
        self.config = config
        self.diagnostic_policy = diagnostic_policy(config)
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
        if not any(path.is_relative_to(Path(root)) for root in [*self.config['read_roots'], str(self.root), str(Path.cwd())]):
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
            try:
                data = self.path(value).read_bytes()
            except FileNotFoundError as error:
                raise ValueError('Missing source for ' + name + ': files must map relative filenames to existing source file paths, not file contents or hashes') from error
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
        pending = []
        for path in sorted(self.root.glob('*/launch-intent.json')):
            run = path.parent.name
            if any(item.get('launch_run') == run for item in self.state['handles']):
                continue
            if self.committed_launch_rejection(run) is None:
                pending.append({'run': run, **json.loads(path.read_text())})
        return pending

    def validate_launch_rejection(self, folder, receipt):
        """Revalidate exact original identities, not absence of a PID or attempt."""
        run = folder.parent.name
        if folder != self.root / run / 'driver' or not re.fullmatch('[a-f0-9]{64}', run):
            raise ValueError('Invalid preflight receipt location')
        request = json.loads((folder / 'request.json').read_text())
        intent_path = folder.parent / 'launch-intent.json'
        intent = json.loads(intent_path.read_text())
        name = intent['driver']; driver = self.config['drivers'].get(name, {})
        if driver.get('kind') != 'launch' or driver.get('preflight_rejection_protocol') != PREFLIGHT_PROTOCOL:
            raise ValueError('Launch driver did not opt into the preflight protocol')
        action_id = request.get('action_id')
        if (not isinstance(action_id, str) or hashlib.sha256(action_id.encode()).hexdigest() != run
                or intent.get('action_id') != action_id or request.get('role') != 'E'
                or request.get('output') != str(folder) or intent.get('output') != str(folder)
                or encoded(request.get('source')) != encoded(intent.get('source'))
                or encoded(request.get('contract')) != encoded(intent.get('contract'))
                or encoded(request.get('binding')) != encoded(intent.get('binding'))):
            raise ValueError('Preflight request differs from the original launch intent')
        configuration_sha = digest(driver)
        if (intent.get('driver_configuration_sha256') != configuration_sha
                or intent.get('preflight_rejection_protocol') != PREFLIGHT_PROTOCOL
                or encoded(intent.get('driver_files')) != encoded(driver.get('files'))
                or receipt.get('driver') != name or receipt.get('kind') != 'launch'
                or receipt.get('driver_configuration_sha256') != configuration_sha
                or encoded(receipt.get('driver_files')) != encoded(driver.get('files'))
                or receipt.get('request_sha256') != sha(folder / 'request.json')
                or receipt.get('launch_intent_sha256') != sha(intent_path)
                or receipt.get('source') != request['source']['sha256']
                or receipt.get('contract') != digest(request['contract'])):
            raise ValueError('Preflight driver identity changed')
        self.verify(request['source'])
        self.verify(request['contract']['oracle']['files'])
        for path, expected in driver['files'].items():
            pinned = Path(path)
            if pinned.resolve() != pinned or not pinned.is_file() or sha(pinned) != expected:
                raise ValueError('Preflight pinned driver changed')
        data = validate_rejection(receipt.get('result'), request, folder)
        if encoded(preflight_json((folder / 'stdout.log').read_bytes())) != encoded(data):
            raise ValueError('Preflight receipt differs from original adapter output')
        record = {'schema_version': 'kernelbridge.launch-rejection/v1', 'run': run,
            'action_id': action_id, 'driver': name, 'driver_configuration_sha256': configuration_sha,
            'driver_files_sha256': digest(driver['files']), 'source_sha256': request['source']['sha256'],
            'contract_sha256': digest(request['contract']), 'binding': request['binding'],
            'request_sha256': sha(folder / 'request.json'), 'intent_sha256': sha(intent_path),
            'receipt_sha256': sha(folder / 'receipt.json'), 'rejection_sha256': digest(data)}
        return record, data

    def committed_launch_rejection(self, run):
        """Only a verified receipt-chain commit can settle an old launch intent."""
        candidates = [item for item in self.state.get('launch_rejections', []) if item.get('run') == run]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError('Ambiguous committed launch rejection')
        declared = candidates[0]
        chain = '0' * 64; committed = None
        for event in sorted(self.root.glob('event-*.json')):
            row = json.loads(event.read_text())
            if row['parent'] != chain or row['sha256'] != digest(row['body']):
                raise ValueError('Operator receipt chain changed during preflight verification')
            chain = sha(event)
            receipts = row['body']['receipts']
            if receipts and receipts[-1].get('run') == run:
                if declared not in row['body'].get('launch_rejections', []):
                    raise ValueError('Launch rejection was not committed with its original receipt')
                committed = receipts[-1]
                break
        if committed is None:
            return None  # Receipt/sidecar alone is never evidence of a terminal commit.
        if committed.get('tool') != 'pipeline_run' or committed.get('status') != 'failed':
            raise ValueError('Invalid committed launch rejection status')
        root = self.root / run; folder = root / 'driver'
        manifest_path = root / 'manifest.json'
        if manifest_path.resolve() != manifest_path or sha(manifest_path) != committed['manifest_sha256']:
            raise ValueError('Committed preflight manifest changed')
        manifest = json.loads(manifest_path.read_text())
        # Hash every retained byte, including stdout/stderr and complete diff files.
        for name, expected in manifest.items():
            path = root / name
            if path.resolve() != path or not path.is_relative_to(root) or not path.is_file() or sha(path) != expected:
                raise ValueError('Committed preflight evidence changed: ' + name)
        required = {'result.json', 'launch-intent.json', 'driver/request.json', 'driver/receipt.json',
                    'driver/stdout.log', 'driver/stderr.log'}
        if not required.issubset(manifest):
            raise ValueError('Committed preflight evidence is incomplete')
        receipt = json.loads((folder / 'receipt.json').read_text())
        record, data = self.validate_launch_rejection(folder, receipt)
        if encoded(record) != encoded(declared):
            raise ValueError('Committed launch rejection identity changed')
        if any('driver/' + name not in manifest for name in data['evidence']['artifacts']):
            raise ValueError('Preflight artifacts are not bound by the committed manifest')
        result = json.loads((root / 'result.json').read_text())
        if (result.get('status') != 'failed' or result.get('data', {}).get('failure_phase') != 'preflight'
                or result['data'].get('dispatch_outcome') != 'not_started'
                or result['data'].get('preflight_rejection', {}).get('rejection_sha256') != digest(data)):
            raise ValueError('Committed launch result is not its original preflight rejection')
        return self.preflight_feedback(record, data)

    def preflight_feedback(self, record, data):
        run = record['run']; root = self.root / run
        names = ['launch-intent.json', 'driver/request.json', 'driver/receipt.json',
                 'driver/stdout.log', 'driver/stderr.log', *('driver/' + name for name in data['evidence']['artifacts'])]
        artifacts = {name: {'sha256': sha(root / name), 'size_bytes': (root / name).stat().st_size,
            'read': {'tool': 'pipeline_read', 'arguments': {'run': run, 'path': name}}} for name in names}
        return {'protocol': data['protocol'], 'operation': data['operation'], 'run': run,
                'action_id': record['action_id'], 'source_sha256': record['source_sha256'],
                'binding': record['binding'], 'rejection_sha256': record['rejection_sha256'],
                'failure_phase': 'preflight', 'dispatch_outcome': 'not_started', 'error': data['error'],
                'evidence': data['evidence'], 'artifacts': artifacts,
                'qualification': 'Remote job never started; this rejection is not correctness or performance evidence.'}

    def pending_effects(self):
        pending = []
        for pattern in ('*/driver/effect-intent.json', '*/arm-*/effect-intent.json', '*/diagnostic-*/driver/effect-intent.json'):
            for path in self.root.glob(pattern):
                if not (path.parent / 'receipt.json').is_file():
                    pending.append(dict(json.loads(path.read_text()), intent=str(path), status='unknown'))
        return pending

    def pending_phases(self):
        committed = {row['run'] for row in self.state['receipts']}
        pending = []
        for path in self.root.glob('*/phase-*-intent.json'):
            intent = json.loads(path.read_text())
            if intent['stage'] in {'F', 'G', 'J'} and intent['run'] not in committed:
                pending.append(dict(intent, status='unknown', intent=str(path)))
        return pending

    def pending_diagnostics(self):
        settled = {item['key']: item for item in self.state.get('diagnostic_runs', [])}
        pending = {key: item for key, item in settled.items() if item.get('status') == 'unknown'}
        for path in self.root.glob('*/diagnostic-*/diagnostic-intent.json'):
            intent = json.loads(path.read_text())
            if intent['key'] not in settled:
                pending[intent['key']] = dict(intent, status='unknown', intent=str(path), run=path.parent.parent.name)
        return list(pending.values())

    def matching_handle(self):
        # Changing only the performance comparator does not change a completed
        # source/contract execution. Keep observing its original job identity.
        binding = self.binding()
        return next((row for row in reversed(self.state['handles'])
                     if row['source']['sha256'] == binding['source']
                     and row['binding']['contract'] == binding['contract']), None)

    def driver(self, name, source, folder, role, handle=None, *, contract=None, binding=None, recovery=None, trigger=None):
        driver = self.config['drivers'].get(name)
        if not driver: raise ValueError('Driver is not configured: ' + name)
        if driver['kind'] != 'collect':
            unresolved = [item for item in self.pending_phases() if item['action_id'] != getattr(self, 'action_id', None)]
            unresolved.extend(self.pending_effects())
            own_intent = str(folder.parent / 'diagnostic-intent.json')
            for item in self.pending_diagnostics():
                if (item.get('intent') == own_intent and item.get('driver') == name
                        and item.get('source') == source['sha256'] and item.get('trigger') == trigger
                        and (trigger or {}).get('run') == hashlib.sha256(self.action_id.encode()).hexdigest()):
                    continue  # Only this newly created diagnostic intent may enter.
                unresolved.append(item)
            if unresolved: raise ValueError('Original phase/diagnostic execution is unresolved; inspect its intent before new device work')
        self.verify(source)
        contract = self.state['contract'] if contract is None else contract
        self.verify(contract['oracle']['files'])
        for path, expected in driver['files'].items():
            if sha(path) != expected: raise ValueError('Configured driver changed')
        remaining = getattr(self, 'execution_deadline', float('inf')) - time.monotonic()
        if remaining <= 0: raise ValueError('Bounded evaluation deadline reached before dispatch')
        folder.mkdir()
        request = {'action_id': getattr(self, 'action_id', None), 'source': source, 'role': role, 'contract': contract, 'handle': handle,
                   'binding': self.binding() if binding is None else binding, 'output': str(folder)}
        if math.isfinite(remaining): request['wait_seconds'] = max(0, remaining - 2)
        if recovery is not None: request['recovery'] = recovery
        if trigger is not None: request['trigger'] = trigger
        save(folder / 'request.json', request)
        if driver['kind'] == 'launch':
            if self.pending_launches():
                raise ValueError('Recover the original unresolved launch before starting another task')
            # This intent survives a timeout, parser error, or killed adapter,
            # even before the ordinary receipt chain can commit the result.
            save(folder.parent / 'launch-intent.json', {'driver': name, 'driver_files': driver['files'],
                'source': source, 'contract': contract, 'binding': request['binding'], 'output': str(folder),
                'action_id': self.action_id, 'driver_configuration_sha256': digest(driver),
                **({'preflight_rejection_protocol': driver['preflight_rejection_protocol']}
                   if 'preflight_rejection_protocol' in driver else {})})
        if driver['kind'] in {'check', 'measure', 'diagnostic', 'calibrate'}:
            save(folder / 'effect-intent.json', {'action_id': self.action_id, 'driver': name, 'kind': driver['kind'],
                'source_sha256': source['sha256'], 'role': role, 'output': str(folder)})
        request_bytes = encoded(request)
        request_sha = sha(folder / 'request.json')
        launch_intent_sha = sha(folder.parent / 'launch-intent.json') if driver['kind'] == 'launch' else None
        # No shell interpolation, credentials or implicit gateway. Explicit drivers
        # receive one JSON request and return one JSON result on stdout.
        started = time.time_ns()
        with (folder / 'stdout.log').open('wb') as stdout, (folder / 'stderr.log').open('wb') as stderr:
            result = subprocess.run(driver['argv'], input=request_bytes, stdout=stdout, stderr=stderr,
                cwd=source['root'], env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONDONTWRITEBYTECODE': '1'},
                timeout=min(driver.get('timeout', 600), max(0.001, getattr(self, 'execution_deadline', float('inf')) - time.monotonic())), check=False)
        self.verify(source)
        if result.returncode: raise ValueError('Driver failed; inspect original logs')
        raw = (folder / 'stdout.log').read_bytes()
        if len(raw) > 4 * 1024 * 1024: raise ValueError('Driver result too large')
        data = preflight_json(raw)
        receipt = {'driver': name, 'kind': driver['kind'], 'driver_files': driver['files'],
                   'started_ns': str(started), 'ended_ns': str(time.time_ns()), 'source': source['sha256'],
                   'contract': digest(contract), 'result': data}
        opted_in = driver.get('preflight_rejection_protocol') == PREFLIGHT_PROTOCOL and driver['kind'] == 'launch'
        preflight_claim = isinstance(data, dict) and (
            isinstance(data.get('protocol'), str) and data['protocol'].startswith('kernelbridge.driver-preflight')
            or data.get('status') == 'rejected' or data.get('phase') == 'preflight'
            or data.get('dispatch_outcome') == 'not_started')
        if preflight_claim:
            # A collector cannot turn a refusal-shaped reply plus a fabricated
            # handle into recovery of a previously uncertain launch.
            if not opted_in:
                raise ValueError('Preflight rejection is unsupported for this driver or operation')
            if (sha(folder / 'request.json') != request_sha
                    or sha(folder.parent / 'launch-intent.json') != launch_intent_sha
                    or data.get('request_sha256') != hashlib.sha256(request_bytes).hexdigest()):
                raise ValueError('Preflight proof changed the original dispatched request or intent')
            receipt['driver_configuration_sha256'] = digest(driver)
            receipt.update(request_sha256=request_sha, launch_intent_sha256=launch_intent_sha)
            save(folder / 'receipt.json', receipt)
            record, refusal = self.validate_launch_rejection(folder, receipt)
            raise PreflightRejected(record, self.preflight_feedback(record, refusal))
        save(folder / 'receipt.json', receipt)
        return receipt

    def diagnostic_key(self, kind, name, source):
        return digest({'binding': self.binding(), 'kind': kind, 'scope': self.diagnostic_policy['scope'],
                       'driver': name, 'configuration': self.config['drivers'].get(name), 'source': source['sha256']})

    def verify_diagnostic_artifacts(self, folder, data, source, kind):
        verify_record(folder, data, source['sha256'], self.diagnostic_policy['scope'], kind)

    def diagnostic_cached(self, key):
        for item in reversed(self.state.get('diagnostic_runs', [])):
            if item['key'] != key:
                continue
            cached = copy.deepcopy(item)
            if item['status'] == 'completed':
                try:
                    receipt = Path(item['receipt'])
                    if sha(receipt) != item['receipt_sha256']:
                        raise ValueError('Diagnostic receipt changed')
                    self.verify_diagnostic_artifacts(receipt.parent, json.loads(receipt.read_text())['result'],
                                                     self.current(), item['kind'])
                except (OSError, ValueError) as error:
                    cached.update(status='failed', error=str(error))
            cached['reused'] = True
            return cached
        # A killed adapter may not have committed a workflow event. Recover its
        # existing intent rather than launch another device profiler implicitly.
        for path in self.root.glob('*/diagnostic-*/diagnostic-intent.json'):
            intent = json.loads(path.read_text())
            if intent['key'] == key:
                return dict(intent, status='unknown', reused=True, intent=str(path),
                            error='Original diagnostic did not settle; inspect its logs/handle before any retry')
        return None

    def diagnostics_after(self, action, output, status, result):
        policy = self.diagnostic_policy
        tool, args = action['tool'], action['arguments']
        if not policy or not (tool == 'pipeline_pair' or tool == 'pipeline_run' and args.get('stage') == 'F'):
            return None
        if not self.state['candidate']:
            return None
        failed = status != 'completed' or (tool == 'pipeline_run' and not (self.state.get('check') or {}).get('passed'))
        regression = tool == 'pipeline_pair' and status == 'completed' and (
            result.get('speedup', 1) <= 1 and 'speedup' in result or bool(result.get('regressions')))
        reason = diagnostic_reason(policy, failed=failed, regression=regression)
        if reason is None:
            return None
        source = self.current()
        handle = (result.get('result') or {}).get('handle') if isinstance(result, dict) else None
        if not handle:
            original = self.matching_handle()
            handle = original['handle'] if original else None
        trigger = {'run': output.name, 'tool': tool, 'stage': 'F' if tool == 'pipeline_run' else 'G',
                   'reason': reason, 'binding': self.binding(), 'handle': handle}
        reports = []
        for kind in ('ir', 'profiler'):
            name = policy['drivers'].get(kind)
            if not name:
                reports.append({'kind': kind, 'status': 'unavailable', 'reason': 'No diagnostic driver configured', 'trigger': trigger})
                continue
            key = self.diagnostic_key(kind, name, source)
            cached = self.diagnostic_cached(key)
            if cached:
                reports.append(dict(cached, trigger=trigger))
                continue
            unresolved = self.pending_effects() + self.pending_diagnostics() + [
                item for item in self.pending_phases() if item['action_id'] != getattr(self, 'action_id', None)]
            if result.get('evaluation_outcome') == 'unknown' or unresolved:
                reports.append({'kind': kind, 'status': 'blocked', 'reason': 'Original evaluation or diagnostic is unresolved', 'trigger': trigger})
                continue
            folder = output / ('diagnostic-' + kind); folder.mkdir()
            report = {'key': key, 'kind': kind, 'driver': name, 'source': source['sha256'],
                      'scope': policy['scope'], 'trigger': trigger, 'reused': False}
            save(folder / 'diagnostic-intent.json', report)
            try:
                receipt = self.driver(name, source, folder / 'driver', 'J', handle, trigger=trigger)
                data = receipt['result']
                report.update(receipt=str(folder / 'driver/receipt.json'), receipt_sha256=sha(folder / 'driver/receipt.json'))
                if data.get('status') in {'unavailable', 'unknown', 'failed'}:
                    report.update(status=data['status'], reason=data.get('reason', 'Diagnostic did not complete'))
                else:
                    if data.get('status', 'completed') != 'completed':
                        raise ValueError('Invalid diagnostic completion status')
                    self.verify_diagnostic_artifacts(folder / 'driver', data, source, kind)
                    report['status'] = 'completed'
            except subprocess.TimeoutExpired as error:
                report.update(status='unknown', error=str(error), recovery='Inspect the original diagnostic logs/handle; no automatic retry')
            except Exception as error:
                report.update(status='failed', error=str(error))
            save(folder / 'diagnostic-result.json', report)
            self.state.setdefault('diagnostic_runs', []).append(report)
            reports.append(report)
        self.state['diagnostics'] = {'trigger': trigger, 'reports': reports,
            'next_step': 'Read diagnostic artifacts and use pipeline_plan stage C to revise the hypothesis; diagnostic evidence is not acceptance.'}
        return self.state['diagnostics']

    def required_diagnostics_passed(self):
        policy = self.diagnostic_policy
        if not policy or not policy['required']:
            return
        for kind in ('ir', 'profiler'):
            name = policy['drivers'].get(kind)
            cached = self.diagnostic_cached(self.diagnostic_key(kind, name, self.current())) if name else None
            if not cached or cached['status'] != 'completed':
                raise ValueError('Required source-bound ' + kind + ' diagnostic is missing or incomplete')

    def check_passed(self):
        check = self.state['check']
        if not check or check['binding'] != self.binding() or not check['passed']:
            raise ValueError('Current source needs a complete passing correctness receipt')

    def workflow(self):
        """Bounded executable advice for the model; never dispatch from a status read."""
        s = self.state
        kinds = {}
        for name, driver in self.config['drivers'].items():
            kinds.setdefault(driver['kind'], []).append(name)
        def step(stage, tool, arguments, reason, **extra):
            return {'stage': stage, 'tool': tool, 'arguments': arguments, 'reason': reason, **extra}
        def run(stage, kind, reason, **arguments):
            names = kinds.get(kind, [])
            if not names:
                return step(stage, None, {}, 'No configured ' + kind + ' driver; ' + reason, status='unavailable')
            return step(stage, 'pipeline_run', dict(stage=stage, driver=names[0], **arguments), reason,
                        alternatives=names)
        next_actions = []
        pending = self.pending_launches()
        last = s.get('last_attempt', {})
        refusal = self.committed_launch_rejection(last['run']) if last.get('dispatch_outcome') == 'not_started' else None
        unresolved = self.pending_phases() + self.pending_effects() + self.pending_diagnostics()
        if unresolved:
            next_actions = [step(last.get('stage'), None, {}, 'Original phase or diagnostic execution is unresolved; inspect its retained intent and logs before new device work.', status='blocked', unresolved=unresolved)]
        elif pending:
            next_actions = [run('E', 'collect', 'Recover this original uncertain launch; never launch a replacement.',
                                recover_run=pending[0]['run'])]
        elif last.get('evaluation_outcome') == 'unknown':
            next_actions = [step(last.get('stage'), None, {}, 'Original execution is unresolved; inspect its original logs and handle before continuing.',
                                 status='blocked', run=last['run'])]
        elif (refusal and s.get('contract') and refusal['binding']['contract'] == digest(s['contract'])
                and (refusal['error']['code'] != 'candidate_format'
                     or refusal['binding']['source'] == (s.get('candidate') or {}).get('sha256'))):
            source_error = refusal['error']['code'] == 'candidate_format'
            next_actions = [step('D' if source_error else 'A', 'pipeline_freeze' if source_error else 'pipeline_bind', {},
                ('Launch was rejected before any remote job started. Correct the candidate source format using the retained validation evidence, then freeze the changed source; the task contract remains bound.'
                 if source_error else 'Launch was rejected before any remote job started. Bind the complete declared contract.json exactly; use the exposed baseline/settings tools separately. Do not trim or guess contract fields.'),
                required_arguments=['files', 'origin'] if source_error else ['contract'], failure_phase='preflight', dispatch_outcome='not_started',
                error=refusal['error'], evidence=refusal['artifacts'])]
        elif not s['contract']:
            next_actions = [step('A', 'pipeline_bind', {}, 'Bind the task, pinned independent oracle, complete suite and measurement contract.',
                                 required_arguments=['contract'])]
        elif not s.get('B') or (s.get('archive') and s['stage'] == 'I'
                and s['archive']['binding']['source'] == (s['candidate'] or {}).get('sha256')):
            next_actions = [step('B', 'pipeline_plan', {'stage': 'B'}, 'Record workload families and per-case gaps; use archived successes/failures in the next iteration.',
                                 required_arguments=['record'])]
        elif s['stage'] == 'B' or not s.get('C'):
            next_actions = [step('C', 'pipeline_plan', {'stage': 'C'}, 'Record a falsifiable structural hypothesis grounded in task, SDK, hardware or diagnostic evidence.',
                                 required_arguments=['record'])]
        elif s['stage'] == 'C' or not s['candidate']:
            next_actions = [step('D', 'pipeline_evaluate' if self.config.get('evaluation_enabled') else 'pipeline_freeze', {},
                                 'Implement the hypothesis, then evaluate exact source files once through D/E/F; read its original feedback before changing source.'
                                 if self.config.get('evaluation_enabled') else 'Implement the hypothesis, then freeze exact source files, configuration and origin.',
                                 required_arguments=['files', 'origin'])]
        else:
            try:
                binding = self.binding()
            except (OSError, ValueError) as error:
                return {'next_actions': [step(s['stage'], None, {}, str(error), status='blocked')],
                        'stop_condition': 'Repair source identity before any new execution.'}
            check = s.get('check')
            terminal = self.matching_handle()
            diagnostics = s.get('diagnostics') or {}
            trigger = diagnostics.get('trigger') or {}
            if trigger.get('binding') == binding and any(report.get('status') == 'unknown' for report in diagnostics.get('reports', [])):
                next_actions = [step('J', None, {}, 'Original diagnostic is unresolved; recover its existing evidence before further device work.', status='blocked')]
            elif terminal and terminal.get('collected') and terminal.get('job_status') == 'failed':
                next_actions = [step('C', 'pipeline_plan', {'stage': 'C'},
                                     'The original job is terminal and failed. Read its retained evaluation evidence and revise the hypothesis; do not recollect or relaunch it.',
                                     required_arguments=['record'], evaluation=terminal.get('evaluation'),
                                     original_receipt=terminal['collected'])]
            elif s['stage'] == 'J' or (check and not check['passed']) or (s.get('pair') and (s['pair']['speedup'] <= 1 or s['pair'].get('qualification', {}).get('qualified') is False)) or (
                    last.get('status') == 'failed' and last.get('stage') in {'F', 'G'} and last.get('source') == s['candidate']['sha256']):
                next_actions = [step('C', 'pipeline_plan', {'stage': 'C'}, 'Read the returned IR/profiler/trace artifacts and failed or regressing cases, then revise the hypothesis.',
                                     required_arguments=['record'], diagnostic_reports=diagnostics.get('reports', []))]
            elif not check or check['binding'] != binding:
                handle = self.matching_handle()
                if self.config.get('evaluation_enabled'):
                    next_actions = [step('F', 'pipeline_evaluate', {}, 'Evaluate the current frozen source through the original E job and F receipt; unchanged source reuses existing effects. Follow returned continuation for bounded recovery.')]
                elif kinds.get('launch') and handle is None:
                    next_actions = [run('E', 'launch', 'Launch this frozen source once and retain its original handle/PID/logs.')]
                elif handle and not handle.get('collected'):
                    next_actions = [run('E', 'collect', 'Observe the original job; do not upload or launch again.', handle=handle['handle'])]
                else:
                    next_actions = [run('F', 'check', 'Run build, directed semantics and the declared complete suite. Only the receipt scope establishes official/device qualification.',
                                        **({'handle': handle['handle']} if handle else {}))]
            elif s['baseline'] and not s.get('pair'):
                names = kinds.get('measure', [])
                next_actions = [step('G', 'pipeline_pair' if names else None, {'driver': names[0]} if names else {},
                                     'Run full same-environment ABBA and inspect overall, family and case regressions.' if names else 'No configured measurement driver; performance comparison is unavailable.',
                                     status='ready' if names else 'unavailable', alternatives=names)]
            elif s['incumbent'] != s['candidate']:
                try:
                    self.required_diagnostics_passed()
                except (OSError, ValueError) as error:
                    next_actions = [step('J', None, {}, str(error), status='blocked',
                                         diagnostic_reports=diagnostics.get('reports', []))]
                else:
                    next_actions = [step('H', 'pipeline_promote', {}, 'Promote only complete correct source and a positive full pair. Without baseline, this establishes the first incumbent, not a speedup.')]
            elif kinds.get('calibrate') and (s.get('calibration') or {}).get('binding') != binding:
                next_actions = [run('I', 'calibrate', 'Calibrate the accepted source using the configured driver; report its actual scope and receipt.')]
            else:
                next_actions = [step('I', 'pipeline_archive', {}, 'Archive accepted source bytes and evidence hashes; retain successful mechanisms and failure conditions. This does not publish or submit.',
                                     required_arguments=['knowledge'])]
        return wire_handles({'next_actions': next_actions, 'autonomy': 'Model chooses and invokes tools within the explicit user task; status never dispatches work.',
                'calibration': 'configured' if kinds.get('calibrate') else 'unavailable: no calibration driver configured',
                'baseline': 'selected' if s['baseline'] else 'absent: establish a correct incumbent, then explicitly select it for self-iteration',
                'self_iteration': {'tool': 'pipeline_baseline', 'arguments': {'selection': 'incumbent'},
                                   'when': 'Before the next user-authorized optimization iteration; this invalidates prior comparisons.'}
                                  if s['incumbent'] and not s['baseline'] else None,
                'stop_condition': 'Respect the user-requested scope/rounds; do not loop indefinitely or claim missing official acceptance.'})

    def execute(self, tool, args, output):
        s = self.state
        if tool == 'pipeline_status':
            return dict(s, drivers={key: value['kind'] for key, value in self.config['drivers'].items()},
                        workflow=self.workflow(),
                        diagnostic_policy=self.diagnostic_policy,
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
            for key in ('B', 'C', 'diagnostics', 'archive', 'calibration', 'last_attempt'): s.pop(key, None)
            return {'contract_sha256': digest(contract), 'baseline': None, 'oracle': frozen_oracle['sha256']}
        if not s['contract']: raise ValueError('Call pipeline_bind first')
        if tool == 'pipeline_plan':
            if not args['record']: raise ValueError('Retain workload evidence or a falsifiable hypothesis')
            s['stage'] = args['stage']; s[args['stage']] = args['record']; return args['record']
        if tool == 'pipeline_freeze':
            if not s.get('C'): raise ValueError('Record the implementation hypothesis first')
            if self.config.get('candidate_source') == 'workspace' and any(
                    not Path(path).resolve().is_relative_to(Path.cwd()) for path in args['files'].values()):
                raise ValueError('Candidate files must originate in the isolated current workspace')
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
                collection = next((item for item in s['handles'] if same_handle(item.get('handle'), args.get('handle'))), None)
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
                if any(same_handle(item['handle'], data['handle']) for item in s['handles']):
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
                              'action_id': getattr(self, 'acceptance_action_id', self.action_id),
                              'scope': data.get('scope', 'unspecified')}
                s['pair'] = None
            terminal_phase = data.get('phase') == 'complete'
            legacy_collected = (driver['kind'] == 'collect' and collection and data.get('phase') is None
                                and data.get('status') in {'completed', 'succeeded'})
            if stage == 'E' and (terminal_phase or legacy_collected):
                # Launch acknowledgements are not terminal jobs. Legacy collectors
                # already declare completion through their terminal status.
                if (data.get('job_status') in (None, 'completed', 'succeeded', 'failed', 'cancelled')
                        and data.get('status') in (None, 'completed', 'succeeded', 'failed', 'cancelled')):
                    original_handle = data.get('handle', (collection or {}).get('handle'))
                    retained = next((row for row in s['handles'] if same_handle(row['handle'], original_handle)), None)
                    if retained is not None:
                        retained.update(collected=str(output / 'driver/receipt.json'), job_status=data.get('job_status'))
                        if isinstance(data.get('evaluation'), dict): retained['evaluation'] = data['evaluation']
            if stage == 'I': s['calibration'] = {'binding': self.binding(), 'receipt': str(output / 'driver/receipt.json')}
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
            ratios = aggregate(results, s['contract']['measurement'])
            qualification = qualify(results, s['contract'], self.binding(), source['files'], baseline['files'], ratios)
            overall = math.exp(sum(math.log(r) for r in ratios.values()) / len(ratios))
            families = {}
            for case in s['contract']['suite']:
                families.setdefault(case.get('family', 'all'), []).append(ratios[case['id']])
            s['pair'] = {'binding': self.binding(), 'speedup': overall, 'cases': ratios,
                         'action_id': getattr(self, 'action_id', None),
                         'regressions': [case for case, ratio in ratios.items() if ratio < 1],
                         'families': {name: math.exp(sum(map(math.log, values)) / len(values)) for name, values in families.items()},
                         'environment': env, 'receipts': [str(output / ('arm-' + str(i)) / 'receipt.json') for i in range(4)]}
            if qualification is not None: s['pair']['qualification'] = qualification
            s['stage'] = 'G'; return s['pair']
        if tool == 'pipeline_promote':
            self.check_passed(); pair = s['pair']
            self.required_diagnostics_passed()
            if s['baseline'] and (not pair or pair['binding'] != self.binding() or pair['speedup'] <= 1):
                raise ValueError('Promotion requires a positive complete paired result against the selected baseline')
            if s['contract']['measurement'].get('require_noise') is True and (
                    not pair or pair.get('qualification', {}).get('qualified') is not True):
                raise ValueError('Promotion requires an independently calibrated complete pair above measured noise')
            s.update(incumbent=source, stage='H')
            normalized_contract = copy.deepcopy(s['contract'])
            normalized_contract['oracle']['files'] = s['contract']['oracle']['files']['files']
            receipt_paths = [s['check']['receipt'], *(pair['receipts'] if pair else [])]
            return {'incumbent': source, 'bootstrap': s['baseline'] is None, 'speedup': pair['speedup'] if pair else None,
                    'scope': s['check']['scope'], 'baseline_selection': 'Call pipeline_baseline(selection="incumbent") before the next iteration',
                    'promotion_evidence': {'contract': normalized_contract, 'source_files': source['files'],
                        'baseline_files': s['baseline']['files'] if s['baseline'] else None,
                        'binding': self.binding(), 'check': s['check'], 'pair': pair,
                        'receipts': {path: sha(path) for path in receipt_paths},
                        'promotion_action_id': getattr(self, 'action_id', None)}}
        if tool == 'pipeline_archive':
            self.check_passed()
            if s['incumbent'] != source: raise ValueError('Archive only the accepted source')
            archive = output / 'source.zip'
            with zipfile.ZipFile(archive, 'x') as bundle:
                for name in sorted(source['files']): bundle.write(Path(source['root']) / name, name)
            record = {'source': source, 'archive_sha256': sha(archive), 'binding': self.binding(), 'check': s['check'],
                      'pair': s['pair'], 'knowledge': args['knowledge'], 'published': False}
            save(output / 'release.json', record); s['stage'] = 'I'
            s['archive'] = {'binding': self.binding(), 'receipt': str(output / 'release.json')}
            return record
        raise ValueError('Unknown pipeline tool')

    def apply(self, action, *, acceptance_action_id=None, execution_deadline=float('inf')):
        if action['tool'] == 'pipeline_evaluate':
            from evaluation import evaluate
            return evaluate(self, action)
        self.action_id = action['id']
        self.acceptance_action_id = acceptance_action_id or action['id']
        self.execution_deadline = execution_deadline
        identity = hashlib.sha256(action['id'].encode()).hexdigest()
        output = self.root / identity
        # No implicit rerun after a lost reply. The outer Journal recovers the
        # original effect; an incomplete directory requires reconciliation.
        output.mkdir(exist_ok=False)
        previous = copy.deepcopy(self.state)
        try:
            result = self.execute(action['tool'], action['arguments'], output); status = 'completed'
        except PreflightRejected as error:
            self.state = previous
            self.state.setdefault('launch_rejections', []).append(error.record)
            result = {'error': error.feedback['error']['message'], 'failure_phase': 'preflight',
                      'dispatch_outcome': 'not_started', 'preflight_rejection': error.feedback}
            status = 'failed'
        except Exception as error:
            self.state = previous
            result = {'error': str(error)}; status = 'failed'
            if isinstance(error, subprocess.TimeoutExpired) or any(
                    not (path.parent / 'receipt.json').is_file()
                    for pattern in ('driver/effect-intent.json', 'arm-*/effect-intent.json', 'diagnostic-*/driver/effect-intent.json')
                    for path in output.glob(pattern)):
                result['evaluation_outcome'] = 'unknown'
            if (output / 'launch-intent.json').exists():
                result.update(launch_outcome='unknown', recover_run=identity,
                              recovery='Use a collect driver to recover this original launch; never redispatch')
        diagnostics = None
        if result.get('dispatch_outcome') != 'not_started':
            try:
                diagnostics = self.diagnostics_after(action, output, status, result)
            except Exception as error:
                # Preserve the original check/pair outcome even if diagnostic
                # scheduling itself cannot proceed (for example, changed source).
                diagnostics = {'status': 'blocked', 'reason': str(error), 'evaluation_run': identity}
        # Keep exact F/G acceptance evidence independent from diagnostic feedback.
        if action['tool'] in {'pipeline_run', 'pipeline_pair'}:
            self.state['last_attempt'] = {'run': identity, 'stage': action['arguments'].get('stage', 'G'),
                                          'status': status, 'evaluation_outcome': result.get('evaluation_outcome'),
                                          'source': (self.state['candidate'] or {}).get('sha256'),
                                          **({'dispatch_outcome': 'not_started'} if result.get('dispatch_outcome') == 'not_started' else {})}
        return self.finish(action, output, status, result, diagnostics)

    def finish(self, action, output, status, result, diagnostics=None):
        identity = output.name
        save(output / 'result.json', {'status': status, 'data': result,
            **({'diagnostics': diagnostics} if diagnostics is not None else {})})
        files = {str(p.relative_to(output)): sha(p) for p in output.rglob('*') if p.is_file() and not p.is_symlink()}
        save(output / 'manifest.json', files)
        self.state['receipts'].append({'run': identity, 'tool': action['tool'], 'status': status,
                                       'manifest_sha256': sha(output / 'manifest.json')})
        event = self.root / ('event-%08d.json' % len(self.state['receipts']))
        save(event, {'parent': self.chain, 'body': self.state, 'sha256': digest(self.state)})
        self.chain = sha(event)
        refs = {name: {'path': name, 'sha256': expected, 'size_bytes': (output / name).stat().st_size,
                      'read': {'tool': 'pipeline_read', 'arguments': {'run': identity, 'path': name}}}
                for name, expected in files.items()}
        manifest_ref = {'path': 'manifest.json', 'sha256': sha(output / 'manifest.json'),
                        'read': {'tool': 'pipeline_read', 'arguments': {'run': identity, 'path': 'manifest.json'}}}
        selected = sorted(refs, key=lambda name: (name != 'result.json', name != 'driver/receipt.json', name))[:16]
        summary = {key: result[key] for key in ('error', 'evaluation_outcome', 'launch_outcome', 'recover_run',
                    'failure_phase', 'dispatch_outcome', 'preflight_rejection') if key in result}
        if isinstance(summary.get('error'), str): summary['error'] = summary['error'][:2048]
        if isinstance(result.get('result'), dict) and isinstance(result['result'].get('evaluation'), dict):
            summary['evaluation'] = result['result']['evaluation']
        evaluation_summary = None
        if action['tool'] == 'pipeline_evaluate':
            evaluation_summary = {key: result[key] for key in ('evaluation_status', 'source_sha256', 'source_changed',
                'feedback', 'continuation', 'error', 'check_action_id', 'resume_run', 'unresolved_run', 'original_handle', 'recover_run',
                'failure_phase', 'dispatch_outcome') if key in result}
            for key in ('unresolved_phases', 'unresolved_diagnostics'):
                if key in result: evaluation_summary[key] = result[key][:4]
            evaluation_summary['stages'] = [{key: stage.get(key) for key in ('stage', 'run', 'status')} for stage in result.get('stages', [])]
        return wire_handles({'status': status, 'data': {'run': identity, 'manifest_sha256': sha(output / 'manifest.json'),
            'result': result if len(encoded(result)) < 16000 else 'Read result.json with pipeline_read',
            'summary': summary, 'artifacts': {'manifest': manifest_ref, 'result': refs['result.json'],
                'files': [refs[name] for name in selected], 'file_count': len(refs)},
            **({'evaluation_summary': evaluation_summary} if evaluation_summary is not None else {}),
            **({'promotion_evidence': result['promotion_evidence']}
               if action['tool'] == 'pipeline_promote' and status == 'completed' else {}),
            **({'diagnostics': diagnostics} if diagnostics is not None else {}), 'workflow': self.workflow()}})


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
            expected = record['manifest_sha256'] if name == 'manifest.json' else files.get(name)
            if expected is None or path.resolve() != path or not path.is_relative_to(folder) or sha(path) != expected:
                raise ValueError('Artifact missing or changed')
            offset = args.get('offset', 0); limit = args.get('limit', 8192)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16384:
                raise ValueError('Read offset/limit must be bounded byte counts')
            size = path.stat().st_size
            if offset > size: raise ValueError('Read offset exceeds artifact size')
            with path.open('rb') as stream: stream.seek(offset); data = stream.read(limit)
            encoding = 'utf-8'
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError as error:
                # Never insert replacement characters into source-bound evidence.
                # Prefer a shorter UTF-8 page; arbitrary/binary byte offsets use base64.
                if error.reason == 'unexpected end of data' and error.start > 0:
                    data = data[:error.start]; text = data.decode('utf-8')
                else:
                    encoding = 'base64'; text = base64.b64encode(data).decode('ascii')
            end = offset + len(data)
            return {'status': 'completed', 'data': {'text': text, 'encoding': encoding, 'offset': offset,
                'next_offset': end, 'size_bytes': size, 'sha256': expected, 'eof': end == size,
                'truncated': end < size, 'read': {'tool': 'pipeline_read', 'arguments': {'run': identity, 'path': name}},
                'next_read': {'tool': 'pipeline_read', 'arguments': {'run': identity, 'path': name, 'offset': end, 'limit': limit}} if end < size else None}}
        return pipeline.apply(action)


if __name__ == '__main__':
    try: result = main(json.loads(Path(sys.argv[1]).read_text()), json.load(sys.stdin))
    except Exception as error: result = {'status': 'failed', 'data': {'error': str(error)}}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
