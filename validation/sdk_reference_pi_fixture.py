"""Offline real Pi/Owner/Gateway SDK-reference acceptance; no network model.

Only the provider's network invocation is substituted. Its request preparation,
Pi lifecycle, owner projections, pinned commands, journal and replay remain real.
"""
from pathlib import Path
import argparse
import base64
import hashlib
import importlib.util
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.llm import APIProposer
from kernelcat_executor.capabilities.skills import Skills
from kernelcat_executor.capabilities.workspace_tools import builtin_tools
from kernelcat_executor.contracts.messages import AssistantTurn, TextContent, ToolCall
from kernelcat_executor.contracts.models import Action, Policy, canonical
from kernelcat_executor.contracts.models import Task
from kernelcat_executor.contracts.providers import WireOptions
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.requests import recorded_request
from kernelcat_executor.operations.session import ExecutionSession
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_worker.business_tools import configured_tools, PinnedCommand
from kernelcat_worker.configuration import BusinessToolConfig
from kernelcat_worker.replay import NativeReplayFiles


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'examples/sdk_reference' / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(page):
    return page['text'].encode('utf-8') if page['encoding'] == 'utf-8' else base64.b64decode(page['text'], validate=True)


def turn(name=None, arguments=None):
    return AssistantTurn(content=(ToolCall(id='local-scripted-call', name=name, arguments=arguments),)
                         if name else (TextContent(text='SDK original reference read through EOF.'),),
                         stop_reason='tool_calls' if name else 'stop')


class LocalProvider(APIProposer):
    def __init__(self, scenario, role):
        # Real API serializer, fake network invocation only; no credential lookup.
        super().__init__('openai', 'https://fixture.invalid/v1', 'offline-fixture', 'dummy-fixture-only',
                         wire_options=WireOptions(native_turns=False))
        self.scenario, self.role = scenario, role
        self.calls, self.harnesses, self.contexts, self.wires, self.pages = 0, [], [], [], []
        self.search_pages = []
        self.rebuilt = bytearray()

    def prepare_harness(self, harness):
        self.harnesses.append(json.loads(canonical(harness)))
        return super().prepare_harness(harness)

    def prepare(self, context, tools):
        self.contexts.append(json.loads(canonical(context)))
        assert {'sdk_read', 'sdk_search'} <= {tool.name for tool in tools}
        assert not {'write', 'write_file', 'bash', 'delegate'} & {tool.name for tool in tools}
        return super().prepare(context, tools)

    def invoke_harness(self, request):
        self.wires.append(request)
        self.calls += 1
        if self.scenario.mode == 'child':
            if self.calls == 1:
                return turn('delegate', {'agent': 'kernel-scout', 'task': 'Read the registered generic SDK header through EOF.',
                                         'mode': 'read_only'})
            assert self.calls == 2
            results = [m['content'] for m in request.payload()['messages'] if m['role'] == 'tool']
            assert len(results) == 1 and 'SDK original reference read through EOF.' in results[0]
            delegates = [row for row in self.scenario.store.snapshot()['actions'].values()
                         if row['action']['tool'] == 'delegate']
            assert len(delegates) == 1 and delegates[0]['outcome']['status'] == 'completed', delegates
            assert len(self.scenario.child.rebuilt) == len(self.scenario.source)
            return turn()
        if self.calls == 1:
            return turn('sdk_search', self.scenario.search_initial)
        result = self._parent_result(request, 'SDK reference')
        # The captured HarnessInput and the actual serialized provider request
        # must carry the same original result; neither is reconstructed by us.
        harness_results = [m for m in self.harnesses[-1]['messages']
                           if m['role'] == 'toolResult' and m['toolName'] in ('sdk_read', 'sdk_search')]
        assert json.loads(harness_results[-1]['content'][0]['text']) == result
        if harness_results[-1]['toolName'] == 'sdk_search':
            return self.consume_search(result['outcome']['data'])
        return self.consume(result['outcome']['data'])

    def _parent_result(self, request, name):
        messages = request.payload()['messages']
        results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        assert results, (name, messages)
        value = results[-1]
        assert value['outcome']['status'] == 'completed', value
        return value

    def invoke(self, request):
        self.wires.append(request)
        self.calls += 1
        # Real APIProposer.prepare encoded the actual DelegatedEffects.project
        # context as this user message. Read that wire, not a separately fitted copy.
        context = json.loads(request.payload()['messages'][1]['content'])
        assert context == self.contexts[-1]
        assert context['policy']['observation_bytes'] == 8192
        if self.calls == 1:
            return turn('sdk_search', self.scenario.search_initial)
        observation = context['turns'][-1]['results'][-1]
        assert observation['tool_name'] in ('sdk_read', 'sdk_search')
        assert len(canonical(observation).encode()) <= 8192
        page = observation['data']
        original = self.scenario.store.snapshot()['actions'][observation['action_id']]['outcome']['data']
        if observation['tool_name'] == 'sdk_search':
            assert page['search_progress'] == original['search_progress']
            if not self.search_pages:
                assert page['next_offset'] < original['next_offset'], 'must exercise search report projection'
            return self.consume_search(page)
        if not self.pages:
            assert len(decode(original)) > 8192
            assert page['next_offset'] < original['next_offset'], 'must exercise actual child projection'
        return self.consume(page)

    def consume_search(self, page):
        # Read control data from the actual provider wire, even when the JSON
        # report is incomplete. A search cursor and a report byte offset are
        # independent continuation operations.
        progress = page['search_progress']
        assert progress['mode'] == 'content'
        assert not {'query', 'matches', 'text', 'files'} & progress.keys()
        run = page['read']['arguments']['run']
        raw = (self.scenario.base / 'sdk-tools/receipts' / run / 'search.json').read_bytes()
        assert sha(raw) == page['sha256']
        visible = decode(page)
        assert page['next_offset'] == page['offset'] + len(visible)
        assert visible == raw[page['offset']:page['next_offset']]
        report = json.loads(raw)
        for key in progress.keys() - {'match_count'}:
            assert progress[key] == report[key]
        assert progress['match_count'] == len(report['matches'])
        if self.search_pages:
            assert progress['scan_start'] == self.search_pages[-1]['search_progress']['scan_end']
        else:
            assert progress['scan_start'] == {'file': 0, 'offset': 0}
            assert progress['match_count'] == 50
            assert page['truncated'] and page['next_read']['tool'] == 'sdk_read'
            assert page['next_read']['arguments']['offset'] == page['next_offset']
            assert page['next_read']['arguments']['run'] == run
            assert page['next_read']['arguments']['path'] == 'search.json'
            assert '"next_cursor"' not in page['text'], 'cursor must require the separate visible metadata'
            assert progress['complete'] is False and progress['next_cursor']
        self.search_pages.append(page)
        if progress['complete']:
            assert progress['next_cursor'] is None
            assert [item['search_progress']['match_count'] for item in self.search_pages] == [50, 1]
            return turn('sdk_read', self.scenario.initial)
        return turn('sdk_search', {**self.scenario.search_initial, 'cursor': progress['next_cursor']})

    def consume(self, page):
        assert 'json_preview' not in page and page['text']
        assert page['resource'] == 'vendor'
        assert page['snapshot_id'] == self.scenario.manifest['snapshot_id']
        assert page['sha256'] == sha(self.scenario.source)
        assert page['path'] == 'include/reference.h'
        assert page['source_path'] == str(self.scenario.header)
        assert page['offset'] == len(self.rebuilt)
        data = decode(page)
        assert data == self.scenario.source[page['offset']:page['next_offset']]
        assert page['next_offset'] == page['offset'] + len(data)
        assert page['encoding'] == 'utf-8'
        self.rebuilt.extend(data)
        self.pages.append(page)
        if page['eof']:
            assert page['next_read'] is None
            assert bytes(self.rebuilt) == self.scenario.source
            return turn()
        next_read = page['next_read']
        assert next_read['tool'] == 'sdk_read'
        assert next_read['arguments']['offset'] == len(self.rebuilt)
        assert next_read['arguments']['resource'] == 'vendor'
        assert next_read['arguments']['path'] == 'include/reference.h'
        return turn(next_read['tool'], next_read['arguments'])


class Scenario:
    def __init__(self, base, mode):
        self.base, self.mode = base, mode
        self.runtime_sources = {}
        for name, source in [
            ('kernelcat_executor.operations.context', 'source/packages/executor/src/kernelcat_executor/operations/context.py'),
            ('kernelcat_executor.contracts.text', 'source/packages/executor/src/kernelcat_executor/contracts/text.py'),
            ('kernelcat_executor.operations.delegated', 'source/packages/executor/src/kernelcat_executor/operations/delegated.py'),
            ('kernelcat_executor.operations.gateway', 'source/packages/executor/src/kernelcat_executor/operations/gateway.py'),
            ('kernelcat_executor.capabilities.llm', 'source/packages/executor/src/kernelcat_executor/capabilities/llm.py'),
            ('kernelcat_worker.business_tools', 'source/apps/worker/src/kernelcat_worker/business_tools.py'),
        ]:
            loaded = Path(importlib.import_module(name).__file__)
            assert loaded.read_bytes() == (ROOT / source).read_bytes(), 'Stale runtime: ' + name
            self.runtime_sources[name] = {'path': str(loaded), 'sha256': sha(loaded.read_bytes())}
        self.header = base / 'installed-sdk/include/reference.h'
        self.header.parent.mkdir(parents=True)
        # A generic declaration, not an implementation. The 16384-byte request
        # ends inside a multibyte codepoint, so its real UTF-8 boundary is tested.
        self.source = ('// ' + '资料😀' * 2400 + '\n'
                       + 'int GenericSDKDeclaration();\n' * 51 + '// ORIGINAL_SDK_TAIL\n').encode()
        self.header.write_bytes(self.source)
        self.manifest = module('sdk_pi_snapshot', 'snapshot.py').export_snapshot(
            self.header.parent.parent, base / 'snapshot', name='generic-sdk', version='1.2', target='synthetic-no-device', include=['include'])
        prepared = module('sdk_pi_prepare', 'prepare.py').prepare(
            {'resources': [{'id': 'vendor', 'manifest': str(base / 'snapshot/manifest.json')}]},
            base / 'sdk-tools', sys.executable)
        self.workspace = Workspace(base / 'workspace')
        self.store = AgentJournal(base / 'state/events.jsonl')
        self.store.initialize(None, Policy(context_bytes=262144, observation_bytes=8192))
        self.task = Task(id='sdk-reference-' + mode, scope='sdk-reference', goal='Read the registered generic SDK header through EOF.')
        specs = [BusinessToolConfig.model_validate_json(json.dumps(row)) for row in prepared['business_tools']]
        tools = configured_tools(specs, self.workspace, base / 'tool-state')
        assert all(isinstance(tool, PinnedCommand) for tool in tools)
        self.gateway = Gateway(self.store, [*builtin_tools(self.workspace, Skills(())), *tools, SubagentEffect()], self.task.scope)
        self.parent, self.child = LocalProvider(self, 'parent'), LocalProvider(self, 'child')
        self.initial = {'resource': 'vendor', 'path': 'include/reference.h', 'offset': 0, 'limit': 16384}
        self.search_initial = {'resource': 'vendor', 'query': 'GenericSDKDeclaration', 'limit': 50}
        self.replay = NativeReplayFiles(self.store, base / 'state', self.workspace.root, self.task.id)
        self.session = ExecutionSession(self.gateway, {'primary': self.parent}, [],
            child_proposers={'primary': self.child}, cwd=self.workspace.root,
            argv=(os.environ['KERNELCAT_NODE'], str(ROOT / 'source/apps/agent/src/harness/pi/worker.mjs')),
            external_context=True, replay=self.replay, timeout=120, instruction_files=())

    def run(self):
        result = self.session.run(self.task)
        assert result['status'] == 'finished', result
        reader = self.child if self.mode == 'child' else self.parent
        assert bytes(reader.rebuilt) == self.source
        assert len(reader.pages) >= (4 if self.mode == 'child' else 2)
        assert reader.pages[0]['next_offset'] < 16384
        if self.mode == 'parent':
            assert len(decode(reader.pages[0])) > 8192
        state = self.store.snapshot()
        reads = [row for row in state['actions'].values() if row['action']['tool'] == 'sdk_read']
        searches = [row for row in state['actions'].values() if row['action']['tool'] == 'sdk_search']
        assert len(reads) == len(reader.pages)
        assert len(searches) == len(reader.search_pages) == 2
        assert all(row['terminal'] and row['outcome']['status'] == 'completed' for row in searches)
        assert all(row['terminal'] and row['outcome']['status'] == 'completed' for row in reads)
        self.replay._boundary()
        # All real provider calls have durable wire identity, including child.
        recorded = [recorded_request(self.store, key).fingerprint() for key, row in state['actions'].items()
                    if row['action']['tool'] == '__propose__']
        wires = [wire.fingerprint() for provider in (self.parent, self.child) for wire in provider.wires]
        assert sorted(recorded) == sorted(wires)
        for provider in (self.parent, self.child):
            (self.base / (provider.role + '-harnesses.json')).write_text(canonical(provider.harnesses))
            (self.base / (provider.role + '-contexts.json')).write_text(canonical(provider.contexts))
            (self.base / (provider.role + '-requests.json')).write_text(canonical([wire.model_dump(mode='json') for wire in provider.wires]))
        receipt_root = self.base / 'sdk-tools/receipts'
        before = {str(path.relative_to(receipt_root)): sha(path.read_bytes()) for path in receipt_root.rglob('*') if path.is_file()}
        counts = self.parent.calls, self.child.calls
        source_file = self.base / 'snapshot/files/include/reference.h'
        source_file.chmod(0o600)
        source_file.write_bytes(b'CORPUS_CHANGED_REPLAY_MUST_NOT_SCAN\n')
        action = Action.model_validate_json(canonical(reads[0]['action']))
        replayed = self.gateway.execute(action)
        assert replayed.model_dump(mode='json') == reads[0]['outcome']
        for original_search in searches:
            action = Action.model_validate_json(canonical(original_search['action']))
            assert self.gateway.execute(action).model_dump(mode='json') == original_search['outcome']
        assert self.session.run(self.task)['status'] == 'finished'
        assert (self.parent.calls, self.child.calls) == counts
        assert len(self.store.snapshot()['actions']) == len(state['actions'])
        after = {str(path.relative_to(receipt_root)): sha(path.read_bytes()) for path in receipt_root.rglob('*') if path.is_file()}
        assert before == after, 'cached replay must not create or overwrite scan/page receipts'
        changed = self.gateway.execute(Action(id=self.task.id + '/new-changed-read', task=self.task.id,
                                              tool='sdk_read', arguments=self.initial))
        assert changed.status == 'failed' and 'snapshot_file_changed' in changed.data['reason'], changed
        source_file.write_bytes(self.source)
        source_file.chmod(0o400)
        report = {'status': 'passed', 'mode': self.mode, 'source_bytes': len(self.source), 'source_sha256': sha(self.source),
                  'snapshot_id': self.manifest['snapshot_id'], 'pages': len(reader.pages),
                  'visible_page_bytes': [len(decode(page)) for page in reader.pages],
                  'parent_model_calls': self.parent.calls, 'child_model_calls': self.child.calls,
                  'sdk_read_actions': len(reads), 'wire_requests_verified': len(recorded),
                  'sdk_search_actions': len(searches), 'search_progress_verified_in_actual_wire': True,
                  'search_report_truncated_with_visible_cursor': reader.search_pages[0]['truncated'],
                  'search_cursor_continuation_match_counts': [p['search_progress']['match_count'] for p in reader.search_pages],
                  'gateway_replay_without_rescan': True, 'complete_session_replay_without_model_or_tool': True,
                  'utf8_exact_reassembly': True, 'child_observation_bytes': 8192,
                  'executor_module': str(sys.modules['kernelcat_executor'].__file__),
                  'runtime_sources_match_checkout': self.runtime_sources,
                  'scope': 'Real locked Pi SDK/Owner/Gateway/PinnedCommand with real API request serialization; only provider network invocation is scripted. No external model/SDK execution/NPU.'}
        (self.base / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('parent', 'child'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    print(json.dumps(Scenario(args.output.resolve(), args.mode).run()))
