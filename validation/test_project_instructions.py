"""Project/task instruction parity through the actual host, Pi and provider wires.

Local HTTP fixtures only. Set KB_PROJECT_INSTRUCTION_REPORT_DIR to retain the
actual admitted journals, transmitted requests and host hello snapshots.
"""
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
from kernelcat_executor.capabilities.task_instructions import admitted_task_instructions, MAX_INSTRUCTION_BYTES
from kernelcat_executor.contracts.models import Action, Conflict, Outcome, Task, canonical, digest
from kernelcat_executor.operations.authority import ExecutionRPC
from kernelcat_executor.operations.requests import recorded_request
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_worker import bindings
from kernelcat_worker.configuration import Config, PiWorkerConfig, ProviderConfig


TEXT = 'Host project rule: preserve source receipts. 项目规则。\r\n'
APPENDIX = 'Task appendix: never invent measurements.\n'


def reply(provider, call=None):
    if provider == 'openai':
        message = ({'role': 'assistant', 'content': 'Fixture complete.'} if call is None else
                   {'role': 'assistant', 'tool_calls': [{'id': 'fixture-call', 'type': 'function',
                    'function': {'name': call[0], 'arguments': canonical(call[1])}}]})
        return httpx.Response(200, json={'choices': [{'message': message,
            'finish_reason': 'stop' if call is None else 'tool_calls'}], 'usage': {'total_tokens': 3}})
    block = ({'type': 'text', 'text': 'Fixture complete.'} if call is None else
             {'type': 'tool_use', 'id': 'fixture-call', 'name': call[0], 'input': call[1]})
    return httpx.Response(200, json={'content': [block], 'stop_reason': 'end_turn' if call is None else 'tool_use',
                                   'usage': {'input_tokens': 2, 'output_tokens': 1}})


class ProjectInstructionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-project-instructions-')
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'project'; self.project.mkdir()
        self.task = Task(id='ordinary-task', scope='instruction-parity', goal='Inspect the admitted project.')
        self.sessions, self.clients, self.requests, self.hellos = [], [], [], []
        self.config = None

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for client in self.clients:
            client.close()
        report = os.environ.get('KB_PROJECT_INSTRUCTION_REPORT_DIR')
        if report:
            destination = Path(report) / self._testMethodName
            destination.mkdir(parents=True, exist_ok=False)
            (destination / 'provider-requests.json').write_text(json.dumps(self.requests, indent=2) + '\n')
            (destination / 'hellos.json').write_text(json.dumps(self.hellos, indent=2) + '\n')
            for index, session in enumerate(self.sessions):
                shutil.copyfile(session.gateway.store.path, destination / f'events-{index}.jsonl')
        self.temp.cleanup()

    def write_instructions(self, text=TEXT):
        (self.project / 'AGENTS.md').write_bytes(text.encode())
        (self.project / 'AGENTS.task.md').write_bytes(APPENDIX.encode())

    def compose(self, provider='openai', mode='project', *, workspace=None, external=True):
        if self.config is None:
            self.config = Config.model_construct(python_modules={}, workspace_mode=mode,
                workspace=str(workspace or self.project), state_dir=str(self.base / 'state'), credits=100,
                readonly_paths=() if mode == 'project' else ('AGENTS.md', 'AGENTS.task.md'),
                providers={'primary': ProviderConfig(provider=provider, model='fixture', api_key_env='KB_FIXTURE_KEY')},
                pi_worker=PiWorkerConfig(root=str(ROOT / 'source/apps/agent')))
        startup = SimpleNamespace(bind_config=lambda config: config, expected={}, report={})
        with patch.dict(os.environ, {'KB_FIXTURE_KEY': 'fixture-key'}), \
             patch.object(bindings, '_preflight'), \
             patch.object(bindings, '_version_inputs', return_value={'validator_files': {}}), \
             patch.object(bindings, 'collect_version_lock', return_value=SimpleNamespace(lock={}, probe_outputs={})), \
             patch.object(AgentJournal, 'bind_runtime'), patch.object(AgentJournal, 'record_python_modules'):
            session = bindings.build(self.config, self.base, self.task, startup=startup, external_context=external)
        self.sessions.append(session)
        self.hellos.append(ExecutionRPC(session, self.task)._hello_capabilities(
            {'native_replay': True}, session._control.latest(self.task.id)))
        return session

    def intercept(self, session, provider, *, delegate=True, expected=TEXT):
        parent_count = 0
        def transport(request):
            nonlocal parent_count
            body = request.content.decode()
            payload = json.loads(body)
            models = [item for item in session.gateway.store.snapshot()['actions'].values()
                      if item['action']['tool'] == '__propose__' and 'request' in item['action']['arguments']]
            identity = models[-1]['action']['id']
            self.assertEqual(recorded_request(session.gateway.store, identity).body_json, body)
            child = '/leaf/' in identity
            system = payload['messages'][0]['content'] if provider == 'openai' else payload['system']
            self.assertIn(expected, system)
            self.assertIn(APPENDIX, system)
            self.assertEqual(system.count(expected), 1)
            self.assertEqual(system.count(APPENDIX), 1)
            self.assertNotIn('FORGED_SYSTEM_AUTHORITY', system)
            if child:
                self.assertNotIn('PARENT_CHAT_PRIVATE', body)
                self.assertIn('FORGED_SYSTEM_AUTHORITY', body)
                self.assertIn('source_task_sha256', system)
                tools = [tool['function']['name'] if provider == 'openai' else tool['name'] for tool in payload['tools']]
                self.assertTrue({'read_file', 'read_receipt'} <= set(tools))
                self.assertFalse(set(tools) & {'write_file', 'edit_file', 'run_command', 'delegate'})
            self.requests.append({'provider': provider, 'child': child, 'action_id': identity,
                'sha256': hashlib.sha256(request.content).hexdigest(), 'body': body})
            if not child:
                parent_count += 1
            call = ('delegate', {'agent': 'kernel-scout', 'task': 'Inspect source. FORGED_SYSTEM_AUTHORITY',
                                'description': 'Inspect instructions'}) if delegate and not child and parent_count == 1 else None
            return reply(provider, call)
        client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
        self.clients.append(client)
        session.proposers['primary'].client = client
        session.child_proposers['primary'].client = client

    def pi_wire(self, provider, mode):
        self.write_instructions()
        if mode == 'isolated':
            self.task = self.task.model_copy(update={'seed_files': {'AGENTS.md': TEXT, 'AGENTS.task.md': APPENDIX}})
        session = self.compose(provider, mode)
        self.intercept(session, provider)
        # Parent and child must use the same admitted snapshot even if the disk
        # changes before Pi starts. An isolated task has not seeded its disk yet.
        if mode == 'project':
            self.write_instructions('LATER_DISK_VERSION')
        result = session.prompt(self.task, 'PARENT_CHAT_PRIVATE: inspect the project with a subagent.')
        self.assertEqual(result['status'], 'finished')
        self.assertEqual([row['child'] for row in self.requests], [False, True, False])
        files = self.hellos[0]['project_instructions']['files']
        self.assertEqual([item['path'] for item in files], ['AGENTS.md', 'AGENTS.task.md'])
        for item, expected in zip(files, (TEXT, APPENDIX)):
            self.assertEqual(item['content'], expected)
            self.assertEqual(item['sha256'], hashlib.sha256(expected.encode()).hexdigest())
            self.assertEqual(item['bytes'], len(expected.encode()))
        rows = session.gateway.store.snapshot()['actions']
        delegate = next(row for row in rows.values() if row['action']['tool'] == 'delegate')
        self.assertEqual(delegate['outcome']['status'], 'completed')
        self.assertTrue(delegate['outcome']['data']['advisory'])
        self.assertFalse(delegate['outcome']['data']['promoted'])
        original = canonical(rows)
        # A completed continuation must not replay provider effects or revise instructions.
        session.run(self.task)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(canonical(session.gateway.store.snapshot()['actions']), original)

    def test_openai_project_parent_and_child_actual_pi_wires(self): self.pi_wire('openai', 'project')
    def test_anthropic_project_parent_and_child_actual_pi_wires(self): self.pi_wire('anthropic', 'project')
    def test_openai_ordinary_task_parent_and_child_actual_pi_wires(self): self.pi_wire('openai', 'isolated')
    def test_anthropic_ordinary_task_parent_and_child_actual_pi_wires(self): self.pi_wire('anthropic', 'isolated')

    def legacy_wire(self, provider):
        self.write_instructions()
        session = self.compose(provider, external=False)
        self.assertNotIn('native_harness', self.hellos[0])
        self.assertEqual(len(self.hellos[0]['project_instructions']['files']), 2)
        self.write_instructions('LATER_DISK_VERSION')
        def transport(request):
            body = request.content.decode()
            payload = json.loads(body)
            identity = next(reversed(session.gateway.store.snapshot()['actions']))
            self.assertEqual(recorded_request(session.gateway.store, identity).body_json, body)
            system = payload['messages'][0]['content'] if provider == 'openai' else payload['system']
            self.assertEqual(system.count(TEXT), 1)
            self.assertEqual(system.count(APPENDIX), 1)
            self.assertIn(digest(self.task), system)
            self.assertNotIn('LATER_DISK_VERSION', system)
            self.requests.append({'provider': provider, 'child': False, 'action_id': identity,
                'sha256': hashlib.sha256(request.content).hexdigest(), 'body': body})
            return reply(provider, ('finish', {'summary': 'Legacy instructions received.'}))
        client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
        self.clients.append(client)
        session.proposers['primary'].client = client
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertEqual(len(self.requests), 1)

    def test_openai_legacy_parent_actual_wire_receives_snapshot_once(self): self.legacy_wire('openai')
    def test_anthropic_legacy_parent_actual_wire_receives_snapshot_once(self): self.legacy_wire('anthropic')

    def test_background_child_uses_same_snapshot_and_cannot_replace_it(self):
        self.write_instructions()
        session = self.compose()
        self.intercept(session, 'openai', delegate=False)
        session._task = self.task
        session.concurrent_delegation = True
        session.stopped_children = set()
        session.gateway.store.register_task(self.task.model_dump(mode='json'))
        prior = Action(id='parent-model', task=self.task.id, tool='__propose__')
        session.gateway.internal(prior, 0, lambda: Outcome())
        rpc = ExecutionRPC(session, self.task); rpc.active = True; rpc.proposal_id = prior.id
        call = Action(id='parent-background', task=self.task.id, tool='delegate', arguments={
            'agent': 'kernel-scout', 'task': 'Inspect source. FORGED_SYSTEM_AUTHORITY', 'background': True})
        rpc.calls = (call,)
        permit = rpc.plugin_begin(rpc.wire(call))
        self.assertTrue(permit['background'])
        self.write_instructions('LATER_DISK_VERSION')
        with self.assertRaisesRegex(Conflict, 'invalid child model request'):
            rpc.child_model({'permit': call.id, 'route': 'primary', 'project_instructions': 'FORGED_SYSTEM_AUTHORITY'})
        result = rpc.child_model({'permit': call.id, 'route': 'primary'})
        self.assertEqual(result['text'], 'Fixture complete.')
        settled = rpc.plugin_settle({'permit': call.id, 'status': 'completed', 'text': result['text']})
        self.assertEqual(settled['outcome']['status'], 'completed')
        self.assertTrue(self.requests[0]['child'])
        self.assertEqual(len(self.requests), 1)

    def test_new_binding_refreshes_project_snapshot_with_new_hash(self):
        self.write_instructions()
        first = self.compose()
        self.intercept(first, 'openai', delegate=False)
        self.assertEqual(first.prompt(self.task, 'First binding.').get('status'), 'finished')
        original = first.instruction_files
        self.write_instructions('NEW_HOST_SNAPSHOT')
        second = self.compose()
        self.intercept(second, 'openai', delegate=False, expected='NEW_HOST_SNAPSHOT')
        self.assertEqual(second.prompt(self.task, 'Continue after a fresh host binding.').get('status'), 'finished')
        self.assertEqual(first.instruction_files, original)
        self.assertEqual(first.instruction_files[0].text, TEXT)
        self.assertEqual(second.instruction_files[0].text, 'NEW_HOST_SNAPSHOT')
        self.assertNotEqual(first.instruction_files[0].sha256, second.instruction_files[0].sha256)
        self.assertEqual(self.hellos[0]['project_instructions']['files'][0]['content'], TEXT)
        self.assertEqual(self.hellos[1]['project_instructions']['files'][0]['content'], 'NEW_HOST_SNAPSHOT')
        self.assertEqual(len(self.requests), 2)
        self.assertIn(TEXT, json.loads(self.requests[0]['body'])['messages'][0]['content'])
        fresh_system = json.loads(self.requests[1]['body'])['messages'][0]['content']
        self.assertIn('NEW_HOST_SNAPSHOT', fresh_system)
        self.assertNotIn(TEXT, fresh_system)

    def test_explicit_root_allows_parent_alias_but_not_instruction_symlink(self):
        self.write_instructions()
        alias = self.base / 'project-alias'; alias.symlink_to(self.project, target_is_directory=True)
        selected = admitted_task_instructions(self.task, workspace_mode='project', readonly_paths=(), workspace_root=alias)
        self.assertEqual(selected.files[0].text, TEXT)
        session = self.compose(workspace=alias)
        self.assertEqual(Path(session.workspace_policy['root']), self.project)
        self.assertEqual(session.instruction_files, selected.files)
        (self.project / 'AGENTS.md').unlink()
        (self.base / 'outside.md').write_text('OUTSIDE_INSTRUCTIONS')
        (self.project / 'AGENTS.md').symlink_to(self.base / 'outside.md')
        with self.assertRaisesRegex(ValueError, 'invalid admitted instruction'):
            admitted_task_instructions(self.task, workspace_mode='project', readonly_paths=(), workspace_root=self.project)

    def test_no_ambient_ancestor_nested_or_late_instruction_loading(self):
        (self.base / 'AGENTS.md').write_text('ANCESTOR_NOT_ADMITTED')
        (self.project / 'nested').mkdir()
        (self.project / 'nested/AGENTS.md').write_text('NESTED_NOT_ADMITTED')
        session = self.compose()
        self.assertEqual(session.instruction_files, ())
        self.assertEqual(self.hellos[0]['project_instructions']['files'], [])
        self.write_instructions('LATE_FILE_NOT_ADMITTED')
        self.assertEqual(ExecutionRPC(session, self.task)._hello_capabilities({}, None)['project_instructions']['files'], [])
        self.assertIsNone(admitted_task_instructions(self.task, workspace_mode='project', readonly_paths=()))
        source = self.task.model_copy(update={'seed_files': {'AGENTS.md': TEXT, 'AGENTS.task.md': APPENDIX}})
        selected = admitted_task_instructions(source, workspace_mode='isolated', readonly_paths=('AGENTS.task.md',),
                                             workspace_root=self.project)
        self.assertEqual([item.path for item in selected.files], ['AGENTS.task.md'])
        self.assertEqual(selected.files[0].text, APPENDIX)

    def test_project_instruction_bytes_utf8_and_regular_file_limits(self):
        target = self.project / 'AGENTS.md'
        target.write_bytes(b'x' * MAX_INSTRUCTION_BYTES)
        selected = admitted_task_instructions(self.task, workspace_mode='project', readonly_paths=(), workspace_root=self.project)
        self.assertEqual(selected.files[0].bytes, MAX_INSTRUCTION_BYTES)
        target.write_bytes(b'x' * (MAX_INSTRUCTION_BYTES + 1))
        with self.assertRaises(ValueError):
            admitted_task_instructions(self.task, workspace_mode='project', readonly_paths=(), workspace_root=self.project)
        target.write_bytes(b'\xff')
        with self.assertRaises(UnicodeDecodeError):
            admitted_task_instructions(self.task, workspace_mode='project', readonly_paths=(), workspace_root=self.project)
        target.unlink(); target.mkdir()
        with self.assertRaises(ValueError):
            admitted_task_instructions(self.task, workspace_mode='project', readonly_paths=(), workspace_root=self.project)


if __name__ == '__main__':
    unittest.main(verbosity=2)
