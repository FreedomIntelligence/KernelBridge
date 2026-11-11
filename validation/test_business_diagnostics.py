"""Configured tools run through Pi and Gateway; diagnostic reads bind retained artifacts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent))
import test_runtime as support
sys.path.insert(0, str(support.ROOT / 'examples/cann'))
from diagnostic_tools import tools
from kernelcat_worker.configuration import BusinessToolConfig, SubagentConfig
from kernelcat_worker.business_tools import configured_tools


class BusinessDiagnosticsTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp
    tearDown = support.RuntimeTests.tearDown
    session = support.RuntimeTests.session

    @unittest.skipUnless(Path('/Users/jerry/ops/infra').is_dir(), 'local infra unavailable')
    def test_pi_calls_registered_original_infra(self):
        spec = importlib.util.spec_from_file_location('operator_infra_prepare', support.ROOT / 'examples/operator_infra/prepare.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        specs = module.prepare({'source_root': '/Users/jerry/ops/infra'}, self.base / 'infra', sys.executable)
        registered = configured_tools([BusinessToolConfig.model_validate_json(json.dumps(row)) for row in specs], self.workspace, self.base / 'state')
        session, provider, replay = self.session([('infra_context', {'operator': 'Unique'}),
            ('infra_design', {'operator': 'Unique'})], extra_tools=registered)
        self.assertEqual(session.run(self.task)['status'], 'finished')
        names = {row['name'] for row in specs}
        self.assertTrue(all(names <= actual for actual in provider.declarations))
        results = [row['outcome'] for row in self.store.snapshot()['actions'].values() if row['action']['tool'] in names]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(row['status'] == 'completed' and row['data']['files'] for row in results), results)
        replay._boundary()

    def test_pi_can_parse_read_and_render_registered_llvm_evidence(self):
        evidence = self.base / 'evidence'; identity = 'b' * 64
        folder = evidence / 'diagnostics' / identity; folder.mkdir(parents=True)
        files = []
        for name, source in [('before.ll', 'define void @kernel() {\n ret void\n}\n'),
                             ('after.ll', 'define void @kernel() {\n call void @op()\n ret void\n}\n')]:
            (folder / name).write_text(source)
            files.append({'path': name, 'bytes': len(source), 'sha256': hashlib.sha256(source.encode()).hexdigest()})
        (folder / 'manifest.json').write_text(json.dumps({'collection': identity, 'files': files}))
        command = self.base / 'diagnostic-command.py'
        command.write_text('import sys,json\n' + f'sys.path.insert(0,{str(support.ROOT / "examples/cann/tools")!r})\n'
                           + 'from diagnostics import main\n' + f'print(json.dumps(main({{"state":{str(evidence)!r}}})))\n')
        pin = {'path': str(command), 'sha256': hashlib.sha256(command.read_bytes()).hexdigest()}
        specifications = [BusinessToolConfig.model_validate_json(json.dumps(row)) for row in tools([str(Path(sys.executable).resolve()), '-B', str(command)], [pin])]
        registered = configured_tools(specifications, self.workspace, self.base / 'state')
        session, provider, replay = self.session([
            ('diagnostics_parse', {'collection': identity, 'path': 'before.ll'}),
            ('diagnostics_read', {'collection': identity, 'path': 'after.ll', 'limit': 1024}),
            ('diagnostics_render', {'before_collection': identity, 'before_path': 'before.ll', 'after_collection': identity, 'after_path': 'after.ll'}),
        ], extra_tools=registered)
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertTrue(all({'ir_collect', 'profiler_collect', 'diagnostics_read', 'diagnostics_parse', 'diagnostics_render'} <= names for names in provider.declarations))
        outcomes = [item['outcome'] for item in self.store.snapshot()['actions'].values()
                    if item['action']['tool'] in ('diagnostics_parse', 'diagnostics_read', 'diagnostics_render')]
        self.assertEqual(len(outcomes), 3)
        self.assertTrue(all(item['status'] == 'completed' for item in outcomes), outcomes)
        self.assertEqual(outcomes[0]['data']['report']['definitions'], 1)
        self.assertIn('@op', outcomes[1]['data']['text'])
        self.assertTrue(list(evidence.glob('diagnostics/*/ir.html')))
        replay._boundary()
        command.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            configured_tools(specifications, self.workspace, self.base / 'state')

    def test_business_tools_cannot_replace_pi_base_tools(self):
        file = self.base / 'tool.py'; file.write_text('pass')
        spec = BusinessToolConfig(name='bash', description='bad override', argv=(sys.executable, str(file)),
            parameters={'type':'object'}, files=({'path':str(file), 'sha256':hashlib.sha256(file.read_bytes()).hexdigest()},))
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            configured_tools([spec], self.workspace, self.base / 'state')

    def test_managed_goal_catalogue_executes_the_same_configured_tools(self):
        from kernelcat_worker.runtime import RuntimeService
        from kernelcat_executor.capabilities.skills import Skills
        from kernelcat_executor.contracts.models import Action
        command = self.base / 'probe.py'
        command.write_text('import json,sys\na=json.load(sys.stdin)\nprint(json.dumps({"status":"completed","data":{"value":a["arguments"]["value"]}}))\n')
        specification = BusinessToolConfig(name='operator_probe', description='Bounded operator probe',
            argv=(str(Path(sys.executable).resolve()), str(command)),
            parameters={'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']},
            files=({'path': str(command), 'sha256': hashlib.sha256(command.read_bytes()).hexdigest()},))
        service = object.__new__(RuntimeService)
        service.base = self.base
        service.config = SimpleNamespace(business_tools=(specification,), state_dir='state', rsi=None, self_evolution=None, subagents=SubagentConfig())
        tools = service._tools(self.store, self.workspace, Skills(()), None)
        probe = next(tool for tool in tools if tool.spec.name == 'operator_probe')
        outcome = probe.execute(Action(id='managed/probe', task='test', tool='operator_probe', arguments={'value': 17}))
        self.assertEqual(outcome.status, 'completed')
        self.assertEqual(outcome.data['value'], 17)
        service.config.subagents = SubagentConfig(enabled=False)
        disabled = service._tools(self.store, self.workspace, Skills(()), None)
        self.assertNotIn('delegate', {tool.spec.name for tool in disabled})
