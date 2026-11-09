"""Real Python Journal -> NDJSON -> LoopX -> Pi process integration; no paid model."""
from pathlib import Path
import hashlib
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for source in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(source))

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.skills import Skills
from kernelcat_executor.capabilities.workspace_policy import WorkspaceGuard
from kernelcat_executor.capabilities.workspace_tools import builtin_tools, FunctionTool, schema
from kernelcat_executor.contracts.models import Task, Policy, Outcome, ToolSpec, canonical, Action
from kernelcat_executor.contracts.messages import AssistantTurn, ToolCall, TextContent
from kernelcat_executor.contracts.requests import ModelRequest
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.session import ExecutionSession
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_worker.replay import NativeReplayFiles
from kernelcat_knowledge.storage.filesystem import RawReader


class ScriptedProvider:
    def __init__(self, calls):
        self.calls = iter(calls)
        self.declarations = []

    def prepare_harness(self, harness):
        self.declarations.append({tool['name'] for tool in harness['tools']})
        return ModelRequest(provider='openai', endpoint='https://example.invalid/chat/completions',
            body_json=canonical({'tools': [{'type': 'function', 'function': tool} for tool in harness['tools']]}))

    def invoke_harness(self, request):
        call = next(self.calls, None)
        if isinstance(call, list):
            return AssistantTurn(content=tuple(ToolCall(id=f'provider-{index}',name=item[0],arguments=item[1])
                for index,item in enumerate(call)),stop_reason='tool_calls')
        return (AssistantTurn(content=(TextContent(text='Finished the regression scenario.'),), stop_reason='stop')
                if call is None else AssistantTurn(content=(ToolCall(id='provider-call', name=call[0], arguments=call[1]),), stop_reason='tool_calls'))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-pi-integration-')
        self.base = Path(self.temp.name).resolve()
        self.workspace = Workspace(self.base / 'workspace')
        self.store = AgentJournal(self.base / 'state/events.jsonl')
        self.store.initialize(300, Policy(max_steps=25, context_bytes=262144))
        self.task = Task(id='test', scope='test', goal='Implement solution.py; keep reference read-only.')
        (self.workspace.root / 'reference').mkdir()
        (self.workspace.root / 'reference/spec.txt').write_text('immutable')

    def tearDown(self):
        self.temp.cleanup()

    def session(self, calls):
        provider = ScriptedProvider(calls)
        evaluator = FunctionTool(ToolSpec(name='evaluate', description='Evaluate the candidate.',
            parameters=schema({}, []), credits=2), lambda _: Outcome(data={'correctness': True, 'receipt_fixture': True}))
        knowledge = FunctionTool(ToolSpec(name='knowledge_search', description='Find kernel knowledge.',
            parameters=schema({'query': {'type': 'string'}}, ['query']), credits=1),
            lambda _: Outcome(data={'hits': ['a trusted reference']}))
        number = FunctionTool(ToolSpec(name='number_probe',description='Return a JSON number.',parameters=schema({'value':{'type':'number'}},['value']),credits=1),lambda a:Outcome(data=a.arguments))
        gateway = Gateway(self.store, [*builtin_tools(self.workspace, Skills(())), evaluator, knowledge, number], self.task.scope,
            (WorkspaceGuard(self.workspace.root, ('solution.py',)),))
        replay = NativeReplayFiles(self.store, self.base / 'state', self.workspace.root, self.task.id)
        session = ExecutionSession(gateway, {'primary': provider}, [], cwd=self.workspace.root,
            argv=(shutil.which('node'), str(ROOT / 'source/apps/agent/src/harness/pi/worker.mjs')),
            external_context=True, replay=replay, timeout=60,
            workspace_policy={'root': str(self.workspace.root), 'writable_paths': ['solution.py'],
                              'readonly_paths': ['reference'], 'codemode': False})
        return session, provider, replay

    def test_native_files_business_tools_invalid_call_and_recovery(self):
        session, provider, replay = self.session([
            ('write', {'path': 'solution.py', 'content': 'x = 1\n'}),
            ('edit', {'path': 'solution.py', 'edits': [{'oldText': 'x = 1', 'newText': 'x = 2'}]}),
            ('read', {'path': 'solution.py'}),
            ('write', {'path': 'reference/spec.txt', 'content': 'bad'}),
            ('exec_command', {'cmd': 'true'}),
            ('write', {'path': 'solution.py'}),
            ('read', {'path': 42}),
            ('knowledge_search', {'query': 'vector add'}),
            ('evaluate', {}),
            ('read_receipt', {'action_id': 'test/step/8/model/primary/call/0'}),
        ])
        result = session.run(self.task)
        self.assertEqual(result['status'], 'finished')
        self.assertEqual((self.workspace.root / 'solution.py').read_text(), 'x = 2\n')
        self.assertEqual((self.workspace.root / 'reference/spec.txt').read_text(), 'immutable')
        self.assertTrue(all({'read','write','edit','bash','find','grep','ls','evaluate','knowledge_search','read_receipt'} <= names
                            for names in provider.declarations))
        self.assertTrue(all(not names & {'codemode','tool_search','read_file','write_file','edit_file'} for names in provider.declarations))
        state = self.store.snapshot()
        unexecuted = [row for row in state['actions'].values() if row.get('outcome', {}).get('data', {}).get('unexecuted')]
        self.assertEqual(len(unexecuted), 3)
        self.assertTrue(all(row['terminal'] and row['outcome']['status'] == 'failed' for row in unexecuted))
        replay._boundary()
        # A complete persisted Pi checkpoint can be reopened without another model call.
        before = len(provider.declarations)
        resumed = session.run(self.task)
        self.assertEqual(resumed['status'], 'finished')
        self.assertEqual(len(provider.declarations), before)

    def test_numeric_business_and_native_arguments_survive_js_roundtrip(self):
        session, _, replay = self.session([
            ('number_probe', {'value': 0.0}),
            ('number_probe', {'value': 1.0}),
            ('read', {'path': 'reference/spec.txt', 'limit': 1.0}),
        ])
        self.assertEqual(session.run(self.task)['status'], 'finished')
        rows=[r for r in self.store.snapshot()['actions'].values() if r['action']['tool']=='number_probe']
        self.assertEqual([r['outcome']['data']['value'] for r in rows],[0.0,1.0])
        self.assertTrue(all(type(r['action']['arguments']['value']) is float for r in rows))
        replay._boundary()

    def test_numeric_wire_equivalence_preserves_type_and_exact_value(self):
        from kernelcat_executor.contracts.models import same_json_value
        self.assertTrue(same_json_value({'a':[0.0,1.0]}, {'a':[0,1]}))
        self.assertFalse(same_json_value({'a':False},{'a':0}))
        self.assertFalse(same_json_value({'a':'0'},{'a':0}))
        self.assertFalse(same_json_value(2**53+1,float(2**53+1)))
        self.assertFalse(same_json_value(float('nan'),float('nan')))

    def test_legacy_reference_write_is_denied_without_rsi(self):
        session, _, _ = self.session([])
        self.store.register_task(self.task.model_dump(mode='json'))
        path = self.workspace.root / 'reference/spec.txt'
        outcome = session.gateway.execute(Action(id='test/legacy',task='test',tool='write_file',arguments={
            'path':'reference/spec.txt','text':'bad','expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
        self.assertEqual(outcome.status,'failed')
        self.assertEqual(path.read_text(),'immutable')

    def test_mixed_finish_turn_is_checkpointed_as_unexecuted(self):
        session, _, replay = self.session([[
            ('finish', {'summary': 'premature'}),
            ('write', {'path': 'solution.py', 'content': 'should not execute'}),
        ]])
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertFalse((self.workspace.root / 'solution.py').exists())
        replay._boundary()
        rows=[r for r in self.store.snapshot()['actions'].values() if r.get('outcome',{}).get('data',{}).get('unexecuted')]
        self.assertEqual(len(rows),2)

    def test_scout_cannot_activate_write_or_bash(self):
        session, provider, replay = self.session([
            ('write', {'path':'solution.py','content':'bad'}),
            ('bash', {'command':'echo bad > solution.py'}),
            ('read', {'path':'reference/spec.txt'}),
        ])
        session.role='scout'
        self.assertEqual(session.run(self.task)['status'],'finished')
        self.assertFalse((self.workspace.root/'solution.py').exists())
        self.assertTrue(all(not names & {'write','edit','bash'} for names in provider.declarations))
        replay._boundary()

    def test_knowledge_grep_cannot_follow_external_symlink(self):
        class Loader:
            def find_by_id(self, _): return None
        raw = self.base / 'knowledge/raw'; raw.mkdir(parents=True)
        outside = self.base / 'private.txt'; outside.write_text('PRIVATE_MARKER')
        (raw / 'link.txt').symlink_to(outside)
        (raw / 'safe.txt').write_text('SAFE_MARKER')
        reader = RawReader(raw_root=raw, repo_root=raw.parent, loader=Loader())
        self.assertIn('error', reader.read('raw/link.txt'))
        self.assertEqual(reader.grep('PRIVATE_MARKER')['returned'], 0)
        self.assertEqual(reader.grep('SAFE_MARKER')['returned'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
