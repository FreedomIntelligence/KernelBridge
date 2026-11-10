"""Configured tools run through Pi and Gateway; diagnostic reads bind retained artifacts."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).parent))
import test_runtime as support
sys.path.insert(0, str(support.ROOT / 'examples/cann'))
from diagnostic_tools import tools
from kernelcat_worker.configuration import BusinessToolConfig
from kernelcat_worker.business_tools import configured_tools


class BusinessDiagnosticsTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp
    tearDown = support.RuntimeTests.tearDown
    session = support.RuntimeTests.session

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
