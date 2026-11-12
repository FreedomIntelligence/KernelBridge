"""Host instruction composition, original provider wires and filesystem capability facts.

HTTP responses are local protocol fixtures, never paid-model or device evidence.
Set KB_CHILD_INSTRUCTION_REPORT_DIR to retain original journals and request bodies.
"""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for source in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(source))

import httpx
from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.llm import APIProposer, SYSTEM
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.capabilities.task_instructions import admitted_task_instructions, MAX_INSTRUCTION_BYTES
from kernelcat_executor.capabilities.workspace_tools import FunctionTool
from kernelcat_executor.contracts.context import ContextOverflow
from kernelcat_executor.contracts.models import Action, Conflict, Outcome, Policy, Task, ToolSpec, canonical, digest
from kernelcat_executor.contracts.providers import WireOptions
from kernelcat_executor.operations.authority import ExecutionRPC
from kernelcat_executor.operations.delegated import DelegatedEffects
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.requests import propose, recorded_request
from kernelcat_executor.operations.session import ExecutionSession
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_worker import bindings
from kernelcat_worker.configuration import Config, PiWorkerConfig, ProviderConfig
from kernelcat_worker.runtime import RuntimeService


class ChildInstructionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-child-instructions-')
        self.root = Path(self.temp.name).resolve()
        self.task = Task(id='operator-source', scope='instruction-test', goal='PARENT_GOAL_PRIVATE',
            contract={'acceptance': {'kind': 'operator_pipeline', 'source_paths': ['candidate.py']}},
            seed_files={'AGENTS.task.md': 'Task rule: retain original receipts.\n',
                        'AGENTS.md': '项目规则：先检查源码。\r\n',
                        'nested/AGENTS.md': 'NESTED_NOT_ADMITTED',
                        'reference.txt': 'REFERENCE_NOT_INSTRUCTIONS'})
        self.slice = admitted_task_instructions(self.task, workspace_mode='isolated',
            readonly_paths=('AGENTS.md', 'AGENTS.task.md', 'reference.txt', 'nested/AGENTS.md'))
        self.clients = []
        self.transmitted = []
        self.store = AgentJournal(self.root / 'events.jsonl')
        self.store.initialize(50, Policy(context_bytes=262144))
        self.child_task = Task(id='operator-source/child/leaf', scope=self.task.scope,
                              goal='Inspect the candidate', contract=self.task.contract)
        self.store.register_task(self.child_task.model_dump(mode='json'))
        self.gateway = Gateway(self.store, [], self.task.scope)

    def tearDown(self):
        for client in self.clients:
            client.close()
        destination = os.environ.get('KB_CHILD_INSTRUCTION_REPORT_DIR')
        if destination:
            output = Path(destination) / self._testMethodName
            output.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(self.store.path, output / 'events.jsonl')
            (output / 'provider-requests.json').write_text(json.dumps(self.transmitted, indent=2) + '\n')
        self.temp.cleanup()

    @staticmethod
    def system(payload, provider):
        return payload['messages'][0]['content'] if provider == 'openai' else payload['system']

    def api(self, provider='openai', native=True, *, instructions=True, identity=None, client=None):
        if client is None:
            def transport(request):
                body = request.content.decode('utf-8')
                if identity:
                    # Admission must already have retained these exact bytes before HTTP dispatch.
                    self.assertEqual(recorded_request(self.store, identity).body_json, body)
                self.transmitted.append({'provider': provider, 'body': body,
                    'sha256': hashlib.sha256(request.content).hexdigest()})
                if provider == 'openai':
                    message = ({'role': 'assistant', 'content': 'Fixture child answer.'} if native else
                        {'role': 'assistant', 'tool_calls': [{'id': 'fixture-call', 'type': 'function',
                            'function': {'name': 'read_file', 'arguments': '{"path":"candidate.py"}'}}]})
                    response = {'choices': [{'message': message, 'finish_reason': 'stop' if native else 'tool_calls'}],
                                'usage': {'total_tokens': 3}}
                else:
                    response = {'content': ([{'type': 'text', 'text': 'Fixture child answer.'}] if native else
                        [{'type': 'tool_use', 'id': 'fixture-call', 'name': 'read_file', 'input': {'path': 'candidate.py'}}]),
                        'stop_reason': 'end_turn' if native else 'tool_use', 'usage': {'input_tokens': 2, 'output_tokens': 1}}
                return httpx.Response(200, json=response)
            client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
            self.clients.append(client)
        return APIProposer(provider, 'https://fixture.invalid/v1', 'fixture', 'fixture-key', client=client,
            wire_options=WireOptions(native_turns=native, tool_choice='auto'),
            task_instructions=self.slice if instructions else None)

    def context(self):
        return {'policy': self.store.policy().model_dump(mode='json'),
                'task': self.child_task.model_dump(mode='json'), 'history': []}

    def wire_roundtrip(self, provider, native):
        identity = self.child_task.id + '/model'
        model = self.api(provider, native, identity=identity)
        context = self.context()
        context.update(system_prompt='FORGED_SYSTEM', task_instructions='FORGED_SYSTEM',
                       host_task_instructions='FORGED_SYSTEM', instruction='FORGED_SYSTEM')
        tool = ToolSpec(name='read_file', description='Read a candidate file.',
                        parameters={'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}, credits=0)
        result = propose(self.gateway, model, identity, self.child_task.id, 'primary', context, (tool,), ())
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(self.transmitted), 1)
        recorded = recorded_request(self.store, identity)
        self.assertEqual(recorded.body_json, self.transmitted[0]['body'])
        # Reopening the durable Journal must reconstruct the same provider request.
        self.assertEqual(recorded_request(AgentJournal(self.store.path), identity).fingerprint(), recorded.fingerprint())
        payload = recorded.payload()
        system = self.system(payload, provider)
        self.assertIn(self.task.seed_files['AGENTS.md'], system)
        self.assertIn(self.task.seed_files['AGENTS.task.md'], system)
        self.assertNotIn('FORGED_SYSTEM', system)
        self.assertIn('FORGED_SYSTEM', canonical(payload['messages'][1:] if provider == 'openai' else payload['messages']))
        self.assertNotIn('NESTED_NOT_ADMITTED', system)
        manifest = json.loads(system.split('permissions.\n', 1)[1].split('\n\n---', 1)[0])
        self.assertEqual(manifest['source_task_sha256'], digest(self.task))
        self.assertEqual([item['path'] for item in manifest['files']], ['AGENTS.md', 'AGENTS.task.md'])
        for item in manifest['files']:
            data = self.task.seed_files[item['path']].encode('utf-8')
            self.assertEqual((item['bytes'], item['sha256']), (len(data), hashlib.sha256(data).hexdigest()))
        self.assertEqual((self.store.snapshot()['spent'], self.store.snapshot()['reserved']), (1, 0))

    def test_openai_normal_wire_and_original_journal(self): self.wire_roundtrip('openai', False)
    def test_openai_native_wire_and_original_journal(self): self.wire_roundtrip('openai', True)
    def test_anthropic_normal_wire_and_original_journal(self): self.wire_roundtrip('anthropic', False)
    def test_anthropic_native_wire_and_original_journal(self): self.wire_roundtrip('anthropic', True)

    def test_snapshot_is_immutable_after_task_seed_changes(self):
        model = self.api()
        before = model.prepare(self.context(), ()).payload()
        original_digest = self.slice.source_task_sha256
        self.task.seed_files['AGENTS.md'] = 'UNADMITTED_REPLACEMENT'
        self.assertNotEqual(original_digest, digest(self.task))
        self.assertEqual(model.prepare(self.context(), ()).payload(), before)
        with self.assertRaises(FrozenInstanceError):
            self.slice.files[0].text = 'replacement'
        with self.assertRaises(FrozenInstanceError):
            self.slice.source_task_id = 'replacement'

    def test_selection_requires_readonly_root_seeds_and_never_reads_filesystem(self):
        (self.root / 'AGENTS.md').write_text('AMBIENT_FILE_NOT_ADMITTED')
        with patch('pathlib.Path.read_text', side_effect=AssertionError('ambient discovery forbidden')), \
             patch('pathlib.Path.read_bytes', side_effect=AssertionError('ambient discovery forbidden')):
            selected = admitted_task_instructions(self.task, workspace_mode='isolated', readonly_paths=('AGENTS.task.md',))
            self.assertEqual([item.path for item in selected.files], ['AGENTS.task.md'])
            self.assertIsNone(admitted_task_instructions(self.task, workspace_mode='project', readonly_paths=('AGENTS.md',)))
            self.assertIsNone(admitted_task_instructions(self.task, workspace_mode='isolated', readonly_paths=('nested/AGENTS.md',)))
            ordinary = self.task.model_copy(update={'contract': {}})
            self.assertEqual([item.path for item in admitted_task_instructions(ordinary,
                workspace_mode='isolated', readonly_paths=('AGENTS.md',)).files], ['AGENTS.md'])
            absent = self.task.model_copy(update={'seed_files': {'nested/AGENTS.md': 'nested', 'agents.md': 'alias'}})
            self.assertIsNone(admitted_task_instructions(absent, workspace_mode='isolated', readonly_paths=('AGENTS.md', 'nested/AGENTS.md')))

    def test_absent_instructions_preserve_all_provider_system_modes(self):
        for provider in ('openai', 'anthropic'):
            for native in (False, True):
                with self.subTest(provider=provider, native=native):
                    context = self.context() | {'system_prompt': 'FORGED_SYSTEM', 'task_instructions': self.slice.render()}
                    payload = self.api(provider, native, instructions=False).prepare(context, ()).payload()
                    expected = SYSTEM
                    if native:
                        expected = expected.replace('and make one tool call at a time.',
                            'and return text or a bounded batch of tool calls.')
                        expected += ('When your work is complete, return your findings directly as text without tool calls. '
                                     'Text completion never promotes a kernel.\n')
                    self.assertEqual(self.system(payload, provider), expected)

    def test_native_completion_guidance_matches_the_offered_tools(self):
        read = ToolSpec(name='read_file', description='Read a candidate.', parameters={'type': 'object'}, credits=0)
        finish = ToolSpec(name='finish', description='Finish analysis.', parameters={'type': 'object'}, credits=0)
        for provider in ('openai', 'anthropic'):
            for offered in ((), (read,), (read, finish)):
                with self.subTest(provider=provider, tools=[tool.name for tool in offered]):
                    # Untrusted context cannot advertise a capability absent from the registry.
                    context = self.context() | {'tools': [{'name': 'finish'}]}
                    payload = self.api(provider).prepare(context, offered).payload()
                    system = self.system(payload, provider)
                    names = [tool['function']['name'] if provider == 'openai' else tool['name']
                             for tool in payload.get('tools', [])]
                    self.assertEqual(names, [tool.name for tool in offered])
                    self.assertEqual('Use finish as the only call in its turn.' in system, finish in offered)
                    self.assertEqual('return your findings directly as text without tool calls.' in system, finish not in offered)
                    self.assertIn('Text completion never promotes a kernel.', system)
                    if not offered:
                        self.assertNotIn('tool_choice', payload)
                        self.assertNotIn('parallel_tool_calls', payload)

    def test_harness_projection_remains_the_authority_for_parent_requests(self):
        harness = {'system_prompt': 'PARENT_NATIVE_SYSTEM', 'messages': [{'role': 'user', 'content': 'parent request'}], 'tools': []}
        for provider in ('openai', 'anthropic'):
            self.assertEqual(self.system(self.api(provider).prepare_harness(harness).payload(), provider), 'PARENT_NATIVE_SYSTEM')

    def test_utf8_file_bound_counts_bytes_and_does_not_truncate(self):
        text = '界' * (MAX_INSTRUCTION_BYTES // 3) + 'x' * (MAX_INSTRUCTION_BYTES % 3)
        task = self.task.model_copy(update={'seed_files': {'AGENTS.md': text}})
        selected = admitted_task_instructions(task, workspace_mode='isolated', readonly_paths=('AGENTS.md',))
        self.assertEqual(selected.files[0].bytes, MAX_INSTRUCTION_BYTES)
        self.assertEqual(selected.files[0].text, text)
        task.seed_files['AGENTS.md'] += 'x'
        with self.assertRaisesRegex(ValueError, '128 KiB'):
            admitted_task_instructions(task, workspace_mode='isolated', readonly_paths=('AGENTS.md',))

    def test_context_overflow_stops_before_admission_or_http(self):
        context = self.context()
        context['policy']['context_bytes'] = 4096
        context['padding'] = ''
        remaining = 4096 - len(canonical(context).encode()) - len(('\n' + self.slice.render()).encode())
        self.assertGreater(remaining, 0)
        context['padding'] = 'x' * remaining
        model = self.api()
        model.prepare(context, ())  # Exact boundary is usable without truncation.
        context['padding'] += 'x'
        with self.assertRaises(ContextOverflow):
            propose(self.gateway, model, 'overflow', self.child_task.id, 'primary', context, (), ())
        self.assertEqual(self.transmitted, [])
        self.assertNotIn('overflow', self.store.snapshot()['actions'])
        self.assertEqual(self.store.snapshot()['spent'], 0)

    def test_factory_binds_host_instructions_to_separate_parent_and_child_transports(self):
        configured = {'primary': ProviderConfig(model='fixture', api_key_env='KB_FIXTURE_KEY')}
        with patch.dict(os.environ, {'KB_FIXTURE_KEY': 'fixture-key'}):
            children = bindings._build_proposers(configured, child=True, task_instructions=self.slice)
            parents = bindings._build_proposers(configured, task_instructions=self.slice)
        child = children['primary'].prepare(self.context(), ()).payload()
        parent = parents['primary'].prepare(self.context(), ()).payload()
        self.assertIn(self.slice.files[0].text, self.system(child, 'openai'))
        self.assertIn(self.slice.files[0].text, self.system(parent, 'openai'))
        self.assertEqual(self.system(parents['primary'].prepare_harness({
            'system_prompt': 'PI_ALREADY_COMPOSED_SYSTEM', 'messages': [], 'tools': []}).payload(), 'openai'),
            'PI_ALREADY_COMPOSED_SYSTEM')
        self.assertIsNot(children['primary'], parents['primary'])

    def test_real_delegated_projection_has_instructions_without_parent_chat(self):
        effect = SubagentEffect()
        def unexpected_tool(_action):
            self.fail('A completed advisory answer must not execute a tool')
        tools = [FunctionTool(ToolSpec(name=name, description='Fixture parent tool.',
                    parameters={'type': 'object'}, credits=0), unexpected_tool)
                 for name in ('read_file', 'finish')]
        gateway = Gateway(self.store, [effect, *tools], self.task.scope)
        self.store.register_task(self.task.model_dump(mode='json'))
        for provider in ('openai', 'anthropic'):
            for mode in ('analysis', 'read_only'):
                with self.subTest(provider=provider, mode=mode):
                    suffix = provider + '-' + mode
                    prior = Action(id='parent-model-' + suffix, task=self.task.id, tool='__propose__',
                                   arguments={'conversation': 'PRIVATE_PARENT_CONVERSATION'})
                    gateway.internal(prior, 0, lambda: Outcome())
                    action = Action(id='parent-delegate-' + suffix, task=self.task.id, tool='delegate',
                                    arguments={'agent': 'kernel-scout', 'task': 'Inspect the admitted candidate', 'mode': mode})
                    identity = action.id + '/leaf/step/0/model/primary'
                    owner = ExecutionSession(gateway, {}, [], cwd=self.root,
                        child_proposers={'primary': self.api(provider, identity=identity)})
                    self.addCleanup(owner.close)
                    owner._task = self.task
                    owner._messages = [{'text': 'PRIVATE_PARENT_CONVERSATION'}]
                    child = DelegatedEffects(owner, action, prior.id)
                    before = len(self.transmitted)
                    result = child.model('primary')
                    self.assertEqual(result['text'], 'Fixture child answer.')
                    self.assertEqual(result['calls'], [])
                    self.assertEqual(child.summary, result['text'])
                    self.assertEqual(child.completion_reason, 'answer')
                    with self.assertRaisesRegex(Conflict, 'final model receipt'):
                        child.model('primary')
                    self.assertEqual(len(self.transmitted), before + 1)
                    body = recorded_request(self.store, identity).body_json
                    self.assertNotIn('PRIVATE_PARENT_CONVERSATION', body)
                    self.assertNotIn('PARENT_GOAL_PRIVATE', body)
                    payload = json.loads(body)
                    system = self.system(payload, provider)
                    self.assertIn(self.slice.files[0].text, system)
                    self.assertNotIn('Use finish as the only call', system)
                    self.assertIn('return your findings directly as text without tool calls.', system)
                    names = [tool['function']['name'] if provider == 'openai' else tool['name']
                             for tool in payload.get('tools', [])]
                    self.assertEqual(set(names), set() if mode == 'analysis' else {'read_file', 'read_receipt'})
                    if mode == 'analysis':
                        self.assertEqual(child.max_steps, 1)
                        self.assertNotIn('tool_choice', payload)

    def actual_case_sensitive(self, root):
        probe = root / 'KbCaseSensitivityProbe'
        probe.write_text('filesystem observation')
        try:
            return not (root / 'kbcaseSensitivityprobe').exists()
        finally:
            probe.unlink()

    def test_legacy_session_exports_actual_case_fact_without_mutating_input(self):
        policy = {'root': str(self.root), 'writable_paths': None, 'readonly_paths': []}
        model = self.api(instructions=False)
        session = ExecutionSession(self.gateway, {'primary': model}, [], cwd=self.root,
                                   workspace_policy=policy, external_context=True)
        self.addCleanup(session.close)
        hello = ExecutionRPC(session, self.child_task)._hello_capabilities({}, None)
        self.assertNotIn('case_sensitive', policy)
        self.assertIs(hello['native_harness']['workspace']['case_sensitive'], self.actual_case_sensitive(self.root))
        for invalid in ('false', 0, None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'boolean'):
                ExecutionSession(self.gateway, {}, [], cwd=self.root, workspace_policy=policy | {'case_sensitive': invalid})

    def config(self, base, mode='isolated'):
        return Config.model_construct(python_modules={}, workspace_mode=mode, workspace=str(base / 'workspace'),
            state_dir=str(base / 'state'), credits=50,
            providers={'primary': ProviderConfig(model='fixture', api_key_env='KB_FIXTURE_KEY')},
            pi_worker=PiWorkerConfig(root=str(ROOT / 'source/apps/agent')))

    def test_unmanaged_composition_exports_actual_fact_and_shared_instructions(self):
        base = self.root / 'composition'; base.mkdir()
        config = self.config(base)
        startup = SimpleNamespace(bind_config=lambda config: config, expected={}, report={})
        # Installation/version admission is tested elsewhere. Keep actual workspace,
        # Gateway, provider factory, ExecutionSession and native capability export.
        with patch.dict(os.environ, {'KB_FIXTURE_KEY': 'fixture-key'}), \
             patch.object(bindings, '_preflight'), \
             patch.object(bindings, '_version_inputs', return_value={'validator_files': {}}), \
             patch.object(bindings, 'collect_version_lock', return_value=SimpleNamespace(lock={}, probe_outputs={})), \
             patch.object(AgentJournal, 'bind_runtime'), patch.object(AgentJournal, 'record_python_modules'):
            session = bindings.build(config, base, self.task, startup=startup, external_context=True)
        self.addCleanup(session.close)
        root = Path(session.workspace_policy['root'])
        hello = ExecutionRPC(session, self.task)._hello_capabilities({}, None)
        self.assertIs(hello['native_harness']['workspace']['case_sensitive'], self.actual_case_sensitive(root))
        self.assertIn(self.slice.files[0].text, self.system(session.child_proposers['primary'].prepare(self.context(), ()).payload(), 'openai'))
        self.assertIn(self.slice.files[0].text,
            self.system(session.proposers['primary'].prepare(self.context(), ()).payload(), 'openai'))

    def test_managed_composition_exports_actual_fact_and_child_instructions(self):
        from kernelcat_worker import runtime
        base = self.root / 'managed'; base.mkdir()
        config = self.config(base)
        workspace = Workspace(base / 'workspace')
        service = RuntimeService.__new__(RuntimeService)
        service.config, service.base, service.store, service.journal = config, base, None, self.store
        service.artifacts = RuntimeArtifacts(base / 'artifacts')
        service.knowledge = SimpleNamespace(open=lambda _: None)
        service.stack = ExitStack(); self.addCleanup(service.stack.close)
        catalogue = Gateway(self.store, [], self.task.scope).specifications()
        owner = SimpleNamespace(tool_names=[tool.name for tool in catalogue],
            version_lock={'lock': {'tool_schema': {'value': 'sha256:' + digest([tool.model_dump(mode='json') for tool in catalogue])}}})
        service._managed_owner = lambda: owner
        service._binding_inputs = lambda state, replacement: ({'trial_id': 'fixture'}, {'task': self.task.model_dump(mode='json')})
        service._workspace = lambda actor: workspace
        service._tools = lambda *args: []
        state = {'trial': {'external_snapshot': {}}, 'stamp': {'run_status': 'active'}}
        with patch.dict(os.environ, {'KB_FIXTURE_KEY': 'fixture-key'}), patch.object(runtime, 'RuntimeStore', return_value=self.store):
            service._bind(state)
        hello = ExecutionRPC(service.host, self.task)._hello_capabilities({}, None)
        self.assertIs(hello['native_harness']['workspace']['case_sensitive'], self.actual_case_sensitive(workspace.root))
        self.assertIn(self.slice.files[0].text, self.system(service.host.child_proposers['primary'].prepare(self.context(), ()).payload(), 'openai'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
