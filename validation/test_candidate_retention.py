import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import test_runtime as support
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.capabilities.workspace_tools import FunctionTool, builtin_tools
from kernelcat_executor.capabilities.skills import Skills
from kernelcat_executor.contracts.models import Action, Outcome, ToolSpec
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_worker.business_tools import configured_tools
from kernelcat_worker.configuration import BusinessToolConfig, CandidateRetention, RequiredCheck
from kernelcat_worker.candidate_retention import RequiredChecksGuard, qualified_source, retained_business_bundle


class CandidateRetentionTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp
    tearDown = support.RuntimeTests.tearDown

    def test_passing_tool_captures_source_before_later_draft_and_checks_digest(self):
        command = self.base / 'check.py'
        command.write_text('import sys,json,hashlib\nfrom pathlib import Path\na=json.load(sys.stdin)\n'
            'print(json.dumps({"status":"completed","data":{"ok":True,"candidate_sha256":'
            'hashlib.sha256(Path("solution.py").read_bytes()).hexdigest() if not a["arguments"].get("stale") else "0"*64}}))\n')
        spec = BusinessToolConfig(name='operator_evaluate', description='Source-bound fixture',
            argv=(str(Path(sys.executable).resolve()), str(command)), parameters={'type': 'object'},
            files=({'path': str(command), 'sha256': hashlib.sha256(command.read_bytes()).hexdigest()},),
            capture_paths=('solution.py',))
        tools = configured_tools([spec], self.workspace, self.base / 'state')
        gateway = Gateway(self.store, tools, 'test')
        self.store.register_task(self.task.model_dump(mode='json'))
        (self.workspace.root / 'solution.py').write_text('verified = 1\n')
        self.assertEqual(gateway.execute(Action(id='test/good', task='test', tool=spec.name, arguments={})).status, 'completed')
        (self.workspace.root / 'solution.py').write_text('stale = 2\n')
        gateway.execute(Action(id='test/stale', task='test', tool=spec.name, arguments={'stale': True}))
        policy = CandidateRetention(tool=spec.name, source_hashes={'solution.py': 'data.candidate_sha256'},
            outcome_schema={'properties': {'data': {'required': ['ok'], 'properties': {'ok': {'const': True}}}}})
        artifacts = RuntimeArtifacts(self.base / 'state/runtime-artifacts')
        source = qualified_source(self.store, artifacts, 'test', policy)
        self.assertEqual(source, [{'path': 'solution.py', 'text': 'verified = 1\n'}])
        self.assertIsNone(qualified_source(self.store, artifacts, 'other-task', policy))
        original = {'schema_version': 'kernelcat.program-bundle/v1', 'entrypoint': 'solution.py',
                    'configuration_source': '{}', 'files': [{'path': 'solution.py', 'text': 'initial\n'}]}
        result = retained_business_bundle(self.workspace, original, store=self.store,
            artifacts=artifacts, task_id='test', retention=policy)
        self.assertIn(source[0], result['files'])
        self.assertEqual((self.workspace.root / 'solution.py').read_text(), 'stale = 2\n')

    def test_baseline_check_gates_edits_until_a_passing_receipt(self):
        self.store.register_task(self.task.model_dump(mode='json'))
        check = RequiredCheck(tool='baseline_check', before_tools=('write_file', 'bash'),
            outcome_schema={'required': ['data'], 'properties': {'data': {'required': ['ok'], 'properties': {'ok': {'const': True}}}}})
        tool = FunctionTool(ToolSpec(name='baseline_check', description='Baseline fixture',
            parameters={'type': 'object'}, credits=1), lambda action: Outcome(data={'ok': action.arguments['ok']}))
        gateway = Gateway(self.store, [*builtin_tools(self.workspace, Skills(())), tool], 'test',
            (RequiredChecksGuard(self.store, 'test', [check]),))
        def edit(identity):
            return gateway.execute(Action(id=identity, task='test', tool='write_file',
                arguments={'path': 'solution.py', 'text': 'new\n', 'expected_sha256': None}))
        rejected = edit('test/before')
        self.assertEqual(rejected.status, 'failed')
        self.assertIn('Run baseline_check', rejected.data['reason'])
        gateway.execute(Action(id='test/failed-check', task='test', tool='baseline_check', arguments={'ok': False}))
        self.assertEqual(edit('test/after-failure').status, 'failed')
        gateway.execute(Action(id='test/passed-check', task='test', tool='baseline_check', arguments={'ok': True}))
        self.assertEqual(edit('test/after-success').status, 'completed')
