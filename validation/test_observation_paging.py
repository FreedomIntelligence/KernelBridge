"""Lossless cursor recovery through real journal/project/provider composition.

HTTP replies are local protocol fixtures, not paid-model or device evidence.
KERNELBRIDGE_PAGING_ORIGINAL_TRIAL enables two read-only historical replays;
KB_PAGING_REPORT_DIR retains the actual transmitted bodies and new journals.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for source in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(source))

import httpx
from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.llm import APIProposer
from kernelcat_executor.capabilities.skills import Skills
from kernelcat_executor.capabilities.workspace_tools import FunctionTool, builtin_tools
from kernelcat_executor.contracts.context import ContextOverflow
from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, Outcome, Policy, Task, canonical, digest
from kernelcat_executor.contracts.providers import WireOptions
from kernelcat_executor.contracts.text import fit_observation_page, text_page
from kernelcat_executor.operations.context import _bounded_preview, _observation, project
from kernelcat_executor.operations.delegated import DelegatedEffects
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.receipts import ReceiptReader
from kernelcat_executor.operations.requests import propose, recorded_request
from kernelcat_executor.operations.session import ExecutionSession
from kernelcat_executor.profiles.store import AgentJournal


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def size(value):
    return len(canonical(value).encode('utf-8'))


def response(provider, call=None):
    if provider == 'openai':
        message = {'role': 'assistant', 'content': 'Fixture complete.'}
        if call:
            message = {'role': 'assistant', 'tool_calls': [{'id': 'fixture-call', 'type': 'function',
                'function': {'name': call[0], 'arguments': canonical(call[1])}}]}
        return httpx.Response(200, json={'choices': [{'message': message,
            'finish_reason': 'tool_calls' if call else 'stop'}], 'usage': {'total_tokens': 3}})
    block = ({'type': 'tool_use', 'id': 'fixture-call', 'name': call[0], 'input': call[1]} if call else
             {'type': 'text', 'text': 'Fixture complete.'})
    return httpx.Response(200, json={'content': [block], 'stop_reason': 'tool_use' if call else 'end_turn',
        'usage': {'input_tokens': 2, 'output_tokens': 1}})


def wire_results(payload, provider):
    if provider == 'openai':
        return [json.loads(message['content']) for message in payload['messages'] if message['role'] == 'tool']
    return [json.loads(block['content']) for message in payload['messages']
            if message['role'] == 'user' and isinstance(message['content'], list)
            for block in message['content'] if block['type'] == 'tool_result']


class ObservationPagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-observation-paging-')
        self.root = Path(self.temp.name).resolve()
        self.workspace = Workspace(self.root / 'workspace')
        self.store = AgentJournal(self.root / 'state/events.jsonl')
        self.store.initialize(500, Policy(context_bytes=2097152, observation_bytes=8192, history_items=128))
        self.task = Task(id='paging-parent', scope='paging-fixture', goal='Read admitted sources.')
        self.store.register_task(self.task.model_dump(mode='json'))
        self.clients, self.sessions, self.transmitted, self.recovery = [], [], [], []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for client in self.clients:
            client.close()
        report = os.environ.get('KB_PAGING_REPORT_DIR')
        if report:
            destination = Path(report) / self._testMethodName
            destination.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(self.store.path, destination / 'events.jsonl')
            (destination / 'provider-requests.json').write_text(json.dumps(self.transmitted, indent=2) + '\n')
            (destination / 'recovery.json').write_text(json.dumps(self.recovery, indent=2) + '\n')
        self.temp.cleanup()

    def api(self, provider, native, handler):
        def transport(request):
            body = request.content.decode('utf-8')
            models = [item for item in self.store.snapshot()['actions'].values()
                      if item['action']['tool'] == '__propose__' and 'request' in item['action']['arguments']]
            identity = models[-1]['action']['id']
            self.assertEqual(recorded_request(self.store, identity).body_json, body)
            self.transmitted.append({'provider': provider, 'native': native, 'action_id': identity,
                                     'sha256': hashlib.sha256(request.content).hexdigest(), 'body': body})
            return handler(json.loads(body))
        client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
        self.clients.append(client)
        return APIProposer(provider, 'https://fixture.invalid/v1', 'fixture', 'fixture-key', client=client,
                           wire_options=WireOptions(native_turns=native, tool_choice='auto'))

    def child(self, model, *, unknown=False):
        tools = builtin_tools(self.workspace, Skills(()))
        if unknown:
            tools = [FunctionTool(tool.spec, lambda _: Outcome(status='unknown', data={'reason': 'fixture'}))
                     if tool.spec.name == 'read_file' else tool for tool in tools]
        gateway = Gateway(self.store, [*tools, SubagentEffect()], self.task.scope)
        prior = Action(id='parent-model', task=self.task.id, tool='__propose__')
        gateway.internal(prior, 0, lambda: Outcome())
        owner = ExecutionSession(gateway, {}, [], cwd=self.workspace.root, child_proposers={'primary': model})
        self.sessions.append(owner)
        owner._task = self.task
        action = Action(id='parent-delegate', task=self.task.id, tool='delegate',
                        arguments={'agent': 'kernel-scout', 'task': 'Read the supplied evidence.', 'mode': 'read_only'})
        return DelegatedEffects(owner, action, prior.id)

    def original_documents(self):
        configured = os.environ.get('KERNELBRIDGE_PAGING_ORIGINAL_TRIAL')
        if not configured:
            self.skipTest('Set KERNELBRIDGE_PAGING_ORIGINAL_TRIAL for the original candidate/ABI replay')
        trial = Path(configured)
        abi = json.loads((trial / 'task.json').read_text())['files']['ABI.md']
        self.assertEqual(sha(abi), '5a4935748ccebb84a4bec4a64700784cb1689485bb2a0d72f2d56eeb422a8e5b')
        candidate_sha = 'a4aa70bea0e1e54c5a68fb8270c379ab9e3c710874e88db3f65b4ac1c782a0a6'
        pages = {}
        # Stop after finding the original two reads; do not scan unrelated sessions.
        with (trial / 'run/state/events.jsonl').open() as stream:
            for line in stream:
                row = json.loads(line)
                data = row.get('outcome', {}).get('data', {})
                if row.get('kind') == 'settled' and data.get('sha256') == candidate_sha:
                    pages[data['offset']] = data['text']
                    if 0 in pages and 8192 in pages:
                        break
        candidate = pages[0] + pages[8192]
        self.assertEqual((len(candidate.encode()), sha(candidate)), (8788, candidate_sha))
        self.assertEqual(len(pages[0]), 8192)
        return {'candidate.py': candidate, 'ABI.md': abi}

    def child_roundtrip(self, provider, documents):
        for path, text in documents.items():
            (self.workspace.root / path).write_bytes(text.encode('utf-8'))
        pending = [{'tool': 'read_file', 'arguments': {'path': path}, 'source': text, 'parts': []}
                   for path, text in documents.items()]
        current = 0
        original_receipts = {}
        receipt_added = False

        def handler(payload):
            nonlocal current, receipt_added
            results = wire_results(payload, provider)
            if results:
                observation = results[-1]
                item = pending[current]
                page = observation['data']
                self.assertLessEqual(size(observation), 8192)
                self.assertNotIn('json_preview', page)
                self.assertEqual(observation['tool_name'], item['tool'])
                self.assertEqual(page['offset'], sum(map(len, item['parts'])))
                self.assertEqual(page['sha256'], sha(item['source']))
                self.assertEqual(page['text'], item['source'][page['offset']:page['offset'] + len(page['text'])])
                self.assertEqual(observation['receipt_sha256'],
                                 digest(self.store.snapshot()['actions'][observation['action_id']]))
                if item['tool'] == 'read_receipt':
                    self.assertEqual(page['action_id'], item['arguments']['action_id'])
                    self.assertEqual(page['receipt_digest'], sha(item['source']))
                item['parts'].append(page['text'])
                self.recovery.append({'tool': item['tool'], 'source_sha256': page['sha256'],
                    'offset': page['offset'], 'next_offset': page['next_offset'],
                    'visible_chars': len(page['text']), 'envelope_bytes': size(observation)})
                if page['next_offset'] is not None:
                    self.assertTrue(page['text'])
                    self.assertEqual(page['next_offset'], sum(map(len, item['parts'])))
                    return response(provider, (item['tool'], {**item['arguments'],
                        'offset': page['next_offset'], 'limit': 8192}))
                self.assertEqual(''.join(item['parts']), item['source'])
                current += 1
                if current == len(pending) and not receipt_added:
                    # Read back the authoritative first full read, not its bounded projection.
                    identity, source = next(iter(original_receipts.items()))
                    pending.append({'tool': 'read_receipt', 'arguments': {'action_id': identity},
                                    'source': source, 'parts': []})
                    receipt_added = True
            if current == len(pending):
                return response(provider)
            item = pending[current]
            return response(provider, (item['tool'], {**item['arguments'], 'offset': 0, 'limit': 8192}))

        child = self.child(self.api(provider, True, handler))
        for _ in range(64):
            result = child.model('primary')
            self.assertNotIn('failed', result)
            if not result['calls']:
                self.assertEqual(result['text'], 'Fixture complete.')
                break
            for call in result['calls']:
                outcome = child.tool(call)
                self.assertEqual(outcome['outcome']['status'], 'completed')
                identity = outcome['action_id']
                original_receipts[identity] = canonical(self.store.snapshot()['actions'][identity])
        else:
            self.fail('The model-visible cursor failed to finish within 64 pages')
        self.assertEqual(current, len(pending))
        self.assertTrue(receipt_added)
        for item in pending:
            self.assertEqual(''.join(item['parts']), item['source'])
        for identity, source in original_receipts.items():
            self.assertEqual(canonical(self.store.snapshot()['actions'][identity]), source)
        for path, source in documents.items():
            self.assertEqual((self.workspace.root / path).read_bytes(), source.encode())
        reopened = AgentJournal(self.store.path)
        for request in self.transmitted:
            self.assertEqual(recorded_request(reopened, request['action_id']).body_json, request['body'])
        self.assertGreater(len(pending[-1]['parts']), 1, 'Receipt recovery must actually paginate')
        self.assertLess(self.recovery[0]['next_offset'], 8192)

    def test_openai_original_candidate_abi_and_receipt_wire_recovery(self):
        self.child_roundtrip('openai', self.original_documents())

    def test_anthropic_original_candidate_abi_and_receipt_wire_recovery(self):
        self.child_roundtrip('anthropic', self.original_documents())

    def test_openai_unicode_escaped_wire_recovery(self):
        self.child_roundtrip('openai', {'candidate.py': '界😀e\u0301"\\\n\r\t\0' * 1700})

    def test_anthropic_unicode_escaped_wire_recovery(self):
        self.child_roundtrip('anthropic', {'candidate.py': '界😀e\u0301"\\\n\r\t\0' * 1700})

    def test_legacy_projection_is_bounded_in_all_four_provider_modes(self):
        text = '界😀"\\\n' * 3500
        (self.workspace.root / 'candidate.py').write_bytes(text.encode())
        gateway = Gateway(self.store, builtin_tools(self.workspace, Skills(())), self.task.scope)
        read = Action(id='legacy-read', task=self.task.id, tool='read_file', arguments={'path': 'candidate.py'})
        self.assertEqual(gateway.execute(read).status, 'completed')
        original = canonical(self.store.snapshot()['actions'][read.id])
        for provider in ('openai', 'anthropic'):
            for native in (False, True):
                with self.subTest(provider=provider, native=native):
                    def handler(payload):
                        anchor = next(message['content'] for message in payload['messages'] if message['role'] == 'user')
                        context = json.loads(anchor)
                        item = context['history'][0]
                        envelope = {key: item[key] for key in ('outcome', 'receipt')}
                        page = item['outcome']['data']
                        self.assertLessEqual(size(envelope), 8192)
                        self.assertEqual(page['text'], text[:page['next_offset']])
                        self.assertEqual(page['sha256'], sha(text))
                        self.assertEqual(item['receipt']['sha256'], sha(original))
                        return response(provider, None if native else ('read_file', {'path': 'candidate.py'}))
                    context = project(self.task, self.store.policy(), self.store.snapshot(), [])
                    outcome = propose(gateway, self.api(provider, native, handler), f'{provider}-{native}',
                        self.task.id, 'primary', context, gateway.specifications(), (read.id,))
                    self.assertEqual(outcome.status, 'completed')
        self.assertEqual(canonical(self.store.snapshot()['actions'][read.id]), original)

    def test_unknown_child_effect_stays_unknown_and_blocks_next_model(self):
        child = self.child(self.api('openai', True,
            lambda _: response('openai', ('read_file', {'path': 'candidate.py'}))), unknown=True)
        result = child.model('primary')
        with self.assertRaises(InDoubt):
            child.tool(result['calls'][0])
        original = canonical(self.store.snapshot()['actions'][child.calls[0].id])
        with self.assertRaisesRegex(Conflict, 'must settle'):
            child.model('primary')
        with self.assertRaises(InDoubt):
            project(child.task, self.store.policy(), self.store.snapshot(), [])
        self.assertEqual(len(self.transmitted), 1)
        self.assertEqual(self.store.snapshot()['actions'][child.calls[0].id]['outcome']['status'], 'unknown')
        self.assertEqual(canonical(self.store.snapshot()['actions'][child.calls[0].id]), original)

    def test_receipt_paging_does_not_grant_cross_task_access(self):
        gateway = Gateway(self.store, [], self.task.scope)
        receipt = Action(id='parent-receipt', task=self.task.id, tool='__fixture__')
        gateway.internal(receipt, 0, lambda: Outcome(data={'text': 'private parent receipt'}))
        original = canonical(self.store.snapshot()['actions'][receipt.id])
        for task in (Task(id='child', goal='child', scope=self.task.scope),
                     Task(id='sibling', goal='sibling', scope=self.task.scope)):
            self.store.register_task(task.model_dump(mode='json'))
            result = gateway.execute(Action(id=task.id + '/read', task=task.id, tool='read_receipt',
                arguments={'action_id': receipt.id, 'offset': 0, 'limit': 8192}))
            self.assertEqual(result.status, 'failed')
            self.assertEqual(result.data['error'], 'receipt unavailable for this task')
        self.assertEqual(canonical(self.store.snapshot()['actions'][receipt.id]), original)

    def test_tool_schema_describes_actual_codepoint_units(self):
        specs = {tool.spec.name: tool.spec for tool in builtin_tools(self.workspace, Skills(()))}
        specs['read_receipt'] = ReceiptReader.spec
        for name in ('read_file', 'read_receipt', 'load_skill'):
            with self.subTest(tool=name):
                spec = specs[name]
                self.assertNotIn('byte-page', spec.description)
                for parameter in ('offset', 'limit'):
                    field = spec.parameters['properties'][parameter]
                    self.assertIn('Unicode code', field['description'])
                    self.assertEqual(field['type'], 'integer')
                self.assertEqual(spec.parameters['properties']['limit']['maximum'], 8192)
                if name != 'load_skill':
                    self.assertIn('Unicode code points, not bytes or lines', spec.description)


class PageEnvelopeTests(unittest.TestCase):
    @staticmethod
    def envelope(data):
        return {'action_id': 'read-1', 'status': 'completed', 'receipt_sha256': 'f' * 64,
                'read_tool': 'read_receipt', 'data': data}

    def test_text_reconstruction_uses_visible_unicode_prefix_and_full_envelope(self):
        for text in ('x' * 15000, '界😀e\u0301"\\\n\r\t\0' * 1700):
            for budget in (512, 1024, 8192):
                with self.subTest(source_sha256=sha(text), budget=budget):
                    offset, parts = 0, []
                    while True:
                        raw = {'path': 'source.txt', **text_page(text, offset, 8192)}
                        original = canonical(raw)
                        page = fit_observation_page(raw, budget, self.envelope)
                        self.assertLessEqual(size(self.envelope(page)), budget)
                        self.assertEqual(canonical(raw), original)
                        self.assertEqual(page['sha256'], sha(text))
                        self.assertEqual(page['text'], text[offset:offset + len(page['text'])])
                        parts.append(page['text'])
                        if page['next_offset'] is None:
                            break
                        self.assertTrue(page['text'])
                        self.assertEqual(page['next_offset'], offset + len(page['text']))
                        offset = page['next_offset']
                    self.assertEqual(''.join(parts), text)

    def test_exact_envelope_boundary_and_eof_clipping(self):
        raw = text_page('😀' * 100)
        exact = size(self.envelope(raw))
        self.assertEqual(fit_observation_page(raw, exact, self.envelope), raw)
        page = fit_observation_page(raw, exact - 1, self.envelope)
        self.assertLess(len(page['text']), len(raw['text']))
        self.assertEqual(page['next_offset'], len(page['text']))
        self.assertTrue(page['truncated'])
        self.assertLessEqual(size(self.envelope(page)), exact - 1)
        self.assertEqual(raw['next_offset'], None)

    def test_generic_preview_fits_escaped_json_and_preserves_original(self):
        value = {'unstructured': '界😀"\\\n\r\t\0' * 3000}
        original = canonical(value)
        for budget in (512, 1024, 8192):
            preview = _bounded_preview(value, budget, self.envelope)
            self.assertLessEqual(size(self.envelope(preview)), budget)
            self.assertTrue(original.startswith(preview['json_preview']))
            self.assertEqual(preview['sha256'], sha(original))
            self.assertEqual(preview['full_bytes'], len(original.encode()))
            self.assertNotIn('next_offset', preview)
        self.assertEqual(canonical(value), original)

    @staticmethod
    def byte_page(data, offset=0, count=None, encoding='base64'):
        piece = data[offset:offset + count] if count is not None else data[offset:]
        text = piece.decode('utf-8') if encoding == 'utf-8' else base64.b64encode(piece).decode('ascii')
        end = offset + len(piece)
        read = {'tool': 'pipeline_read', 'arguments': {'run': 'bound-run', 'path': 'compile.log'}}
        return {'text': text, 'encoding': encoding, 'offset': offset, 'next_offset': end,
                'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                'eof': end == len(data), 'truncated': end < len(data), 'read': read,
                'next_read': None if end == len(data) else {'tool': 'pipeline_read',
                    'arguments': {**read['arguments'], 'offset': end, 'limit': 8192}}}

    def test_utf8_and_base64_byte_pages_reconstruct_and_keep_same_read_source(self):
        for encoding, data in (('utf-8', ('界😀"\\\n' * 3000).encode()),
                               ('base64', bytes(range(256)) * 160)):
            for budget in (1024, 8192):
                with self.subTest(encoding=encoding, budget=budget):
                    offset, parts = 0, []
                    while True:
                        # An original EOF page can be clipped and must gain a same-source next_read.
                        raw = self.byte_page(data, offset, encoding=encoding)
                        original = canonical(raw)
                        page = fit_observation_page(raw, budget, self.envelope)
                        self.assertLessEqual(size(self.envelope(page)), budget)
                        piece = page['text'].encode() if encoding == 'utf-8' else base64.b64decode(page['text'], validate=True)
                        self.assertEqual(piece, data[offset:offset + len(piece)])
                        self.assertEqual(page['next_offset'], offset + len(piece))
                        self.assertEqual(page['sha256'], hashlib.sha256(data).hexdigest())
                        self.assertEqual(canonical(raw), original)
                        parts.append(piece)
                        if page['eof']:
                            self.assertIsNone(page['next_read'])
                            break
                        self.assertTrue(piece)
                        self.assertEqual(page['next_read']['tool'], raw['read']['tool'])
                        for key in ('run', 'path'):
                            self.assertEqual(page['next_read']['arguments'][key], raw['read']['arguments'][key])
                        self.assertEqual(page['next_read']['arguments']['offset'], page['next_offset'])
                        offset = page['next_read']['arguments']['offset']
                    self.assertEqual(b''.join(parts), data)

    def test_existing_byte_continuation_preserves_requested_limit(self):
        raw = self.byte_page(b'x' * 20000, count=8192)
        page = fit_observation_page(raw, 1024, self.envelope)
        self.assertLess(page['next_offset'], 8192)
        self.assertEqual(page['next_read']['arguments'],
                         {'run': 'bound-run', 'path': 'compile.log', 'offset': page['next_offset'], 'limit': 8192})

    def test_invalid_page_contracts_never_receive_fabricated_continuations(self):
        valid = self.byte_page(b'x' * 20000, count=8192)
        mutations = [lambda p: p['next_read']['arguments'].update(run='other-run'),
                     lambda p: p['next_read']['arguments'].update(path='other.log'),
                     lambda p: p['next_read'].update(tool='other_read'),
                     lambda p: p['next_read']['arguments'].update(offset=1),
                     lambda p: p['next_read']['arguments'].update(limit=True),
                     lambda p: p.update(next_read=None),
                     lambda p: p.update(text='not base64!'),
                     lambda p: p.update(text='', next_offset=0),
                     lambda p: p.update(eof=True),
                     lambda p: p.update(next_offset=True)]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                value = copy.deepcopy(valid)
                mutate(value)
                original = canonical(value)
                self.assertIsNone(fit_observation_page(value, 1024, self.envelope))
                self.assertEqual(canonical(value), original)
        end = self.byte_page(b'finished')
        end['next_read'] = valid['next_read']
        self.assertIsNone(fit_observation_page(end, 1024, self.envelope))
        empty = text_page('abc') | {'text': '', 'next_offset': 0, 'truncated': True}
        self.assertIsNone(fit_observation_page(empty, 1024, self.envelope))

    def test_oversized_metadata_returns_recovery_marker_without_fake_cursor(self):
        value = {'path': 'x' * 10000, **text_page('must not be silently skipped')}
        original = canonical(value)
        marker = _observation(value, 512, self.envelope)
        self.assertEqual(marker['reason'], 'page_metadata_exceeds_observation_budget')
        self.assertEqual(marker['sha256'], sha(original))
        self.assertNotIn('text', marker)
        self.assertNotIn('next_offset', marker)
        self.assertNotIn('next_read', marker)
        rendered = self.envelope(marker)
        self.assertEqual(rendered['action_id'], 'read-1')
        self.assertEqual(rendered['read_tool'], 'read_receipt')
        self.assertLessEqual(size(rendered), 512)
        self.assertEqual(canonical(value), original)
        with self.assertRaises(ContextOverflow):
            _observation(value, 512, lambda data: {'action_id': 'x' * 1000, 'data': data})

    def test_metadata_with_no_room_for_one_codepoint_never_reports_progress(self):
        value = text_page('😀😀')
        empty = {**value, 'text': '', 'next_offset': 0, 'truncated': True}
        budget = size(self.envelope(empty))
        with self.assertRaisesRegex(ValueError, 'one character'):
            fit_observation_page(value, budget, self.envelope)
        marker = _observation(value, budget, self.envelope)
        self.assertNotIn('next_offset', marker)
        self.assertNotIn('text', marker)


if __name__ == '__main__':
    unittest.main()
