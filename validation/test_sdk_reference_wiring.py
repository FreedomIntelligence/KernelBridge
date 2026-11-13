"""SDK opt-in preparation and sealed child access; no external model or device."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'validation'))
sys.path.insert(0, str(ROOT / 'examples/cann'))
import test_runtime as support
from test_subagents import Child
from npu_isolation_fixture import preparation_profile
from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_worker.business_tools import configured_tools
from kernelcat_worker.configuration import BusinessToolConfig, SubagentConfig


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = load('sdk_wiring_snapshot', 'examples/sdk_reference/snapshot.py')
sdk_prepare = load('sdk_wiring_prepare', 'examples/sdk_reference/prepare.py')
registry = load('sdk_wiring_registry', 'examples/operator_tools.py')
npu = load('sdk_wiring_npu_prepare', 'examples/scatter/prepare_npu_campaign.py')
cann = load('sdk_wiring_cann_prepare', 'examples/cann/prepare.py')
SDK_TOOLS = {'sdk_search', 'sdk_read'}
MARKER = 'SDK_PUBLIC_DECLARATION_FIXTURE_6cb495'


def sdk_fixture(base):
    source = base / 'installed-sdk'
    (source / 'include').mkdir(parents=True)
    (source / 'include/interface.h').write_text('// ' + MARKER + '\nint PublicInterface();\n')
    snapshot.export_snapshot(source, base / 'snapshot', name='Fixture SDK', version='1.0',
                             target='fixture-only', include=['include'])
    return {'resources': [{'id': 'fixture', 'manifest': str(base / 'snapshot/manifest.json')}]}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-sdk-wiring-')
        self.base = Path(self.temp.name).resolve()
        self.sdk = sdk_fixture(self.base)
        self.transport = self.base / 'transport.py'
        self.transport.write_text('raise AssertionError("preparation must not call transport")\n')
        self.expected = self.base / 'expected.json'
        self.expected.write_text(json.dumps({'modules': [
            {'allowed_package_roots': [(self.base / 'site/kernelcat_worker').as_uri()]}]}))

    def tearDown(self):
        self.temp.cleanup()

    def npu_prepare(self, name, supplied=None, runtime=None):
        profile = self.base / (name + '-npu.json')
        profile.write_text(json.dumps({**preparation_profile(device=1), **(supplied or {})}))
        runtime_path = None
        if runtime is not None:
            runtime_path = self.base / (name + '-runtime.json')
            runtime_path.write_text(json.dumps(runtime))
        args = SimpleNamespace(profile=profile, runtime_profile=runtime_path, task=None,
            output=self.base / name, mode='max', device=1, transport=self.transport,
            remote_root='/not-a-real-device', python=Path(sys.executable),
            max_model_turns=None, online_research=False)
        npu.prepare(args)
        return args.output

    def cann_prepare(self, name, top=None, nested=None):
        profile = {'schema_version': 'kernelbridge.cann-profile/v1', 'python': sys.executable,
                   'node': shutil.which('node'), 'expected_python_modules': str(self.expected),
                   'config': {'providers': {'primary': {'model': 'fixture', 'api_key_env': 'FIXTURE_ONLY'}}}}
        if top is not None:
            profile['sdk_reference'] = top
        if nested is not None:
            profile['config']['sdk_reference'] = nested
        path = self.base / (name + '-profile.json')
        path.write_text(json.dumps(profile))
        output = self.base / name
        # This checks preparation wiring, not installation. Keep the existing
        # installed-worker probe inert; actual tool configs are validated below.
        with patch.object(cann.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as probe:
            cann.prepare(ROOT / 'examples/cann/vector-add/task.json', path, output,
                         'generate', session_only=True)
        for call in probe.call_args_list:
            payload = json.loads(call.kwargs['input'])
            self.assertNotIn('sdk_reference', payload['config'])
        return output

    def assert_sdk_tools(self, tools):
        self.assertEqual({row['name'] for row in tools if row['name'].startswith('sdk_')}, SDK_TOOLS)
        for tool in tools:
            BusinessToolConfig.model_validate_json(json.dumps(tool))

    def test_registry_is_opt_in_and_snapshot_stays_outside_prepared_inputs(self):
        for index, value in enumerate((None, False)):
            output = self.base / f'disabled-{index}'
            self.assertEqual(registry.prepare({'sdk_reference': value}, output, sys.executable), [])
            self.assertFalse(output.exists())
        output = self.base / 'enabled'
        self.assert_sdk_tools(registry.prepare({'sdk_reference': self.sdk}, output, sys.executable))
        self.assertTrue((output / 'sdk_reference/resources.json').is_file())
        self.assertFalse(any(path.name == 'interface.h' for path in output.rglob('*')))

    def test_npu_sources_merge_and_seed_only_readonly_descriptor(self):
        plain = self.npu_prepare('plain')
        baseline = json.loads((plain / 'task.json').read_text())
        for index, (supplied, runtime) in enumerate((
                ({'sdk_reference': self.sdk}, {'config': {}}),
                ({}, {'sdk_reference': self.sdk, 'config': {}}),
                ({}, {'config': {'sdk_reference': self.sdk}}),
                ({'sdk_reference': self.sdk}, {'sdk_reference': self.sdk, 'config': {'sdk_reference': self.sdk}}))):
            with self.subTest(source=index):
                output = self.npu_prepare(f'enabled-{index}', supplied, runtime)
                task = json.loads((output / 'task.json').read_text())
                profile = json.loads((output / 'profile.json').read_text())
                self.assertNotIn('sdk_reference', profile)
                self.assertNotIn('sdk_reference', profile['config'])
                self.assert_sdk_tools(profile['config']['business_tools'])
                self.assertIn('sdk-reference.json', task['readonly'])
                self.assertEqual(task['files']['sdk-reference.json'], (output / 'project/sdk-reference.json').read_text())
                self.assertEqual(set(task['files']) - set(baseline['files']), {'sdk-reference.json'})
                self.assertEqual(task['contract'], baseline['contract'])
                self.assertEqual(task['writable'], ['candidate.py'])
                self.assertNotIn('candidate.py', task['files'])
                self.assertNotIn(MARKER, json.dumps(task))
                self.assertFalse(any(path.name == 'interface.h' for path in (output / 'project').rglob('*')))
                self.assertIsNone(profile['application']['max_model_turns'])
                self.assertFalse((output / 'sdk-reference/receipts').exists())

    def test_npu_disabled_and_conflicting_settings_do_not_create_sdk_effects(self):
        for index, value in enumerate((None, False)):
            output = self.npu_prepare(f'plain-{index}', {'sdk_reference': value},
                                      {'sdk_reference': value, 'config': {'sdk_reference': value}})
            profile = json.loads((output / 'profile.json').read_text())
            task = json.loads((output / 'task.json').read_text())
            self.assertNotIn('sdk-reference.json', task['files'])
            self.assertFalse(SDK_TOOLS & {row['name'] for row in profile['config']['business_tools']})
            self.assertNotIn('sdk_reference', profile['config'])
        for index, (supplied, runtime) in enumerate((
                ({'sdk_reference': self.sdk}, {'sdk_reference': False}),
                ({'sdk_reference': self.sdk}, {'config': {'sdk_reference': False}}),
                ({}, {'sdk_reference': False, 'config': {'sdk_reference': self.sdk}}))):
            with self.subTest(conflict=index), self.assertRaisesRegex(ValueError, 'Conflicting sdk_reference'):
                self.npu_prepare(f'conflict-{index}', supplied, runtime)
            self.assertFalse((self.base / f'conflict-{index}').exists())
        with self.assertRaisesRegex(ValueError, 'snapshot configuration'):
            self.npu_prepare('invalid', {'sdk_reference': True})
        self.assertFalse((self.base / 'invalid').exists())

    def test_cann_consumes_top_and_nested_config_without_copying_sdk_into_seed(self):
        plain = self.cann_prepare('plain')
        baseline = json.loads((plain / 'task.json').read_text())
        self.assertNotIn('sdk-reference.json', baseline['seed_files'])
        self.assertFalse(SDK_TOOLS & {row['name'] for row in json.loads((plain / 'config.json').read_text()).get('business_tools', [])})
        for index, (top, nested) in enumerate(((self.sdk, None), (None, self.sdk), (self.sdk, self.sdk))):
            with self.subTest(source=index):
                output = self.cann_prepare(f'enabled-{index}', top, nested)
                config = json.loads((output / 'config.json').read_text())
                task = json.loads((output / 'task.json').read_text())
                self.assertNotIn('sdk_reference', config)
                self.assert_sdk_tools(config['business_tools'])
                self.assertIn('sdk-reference.json', config['readonly_paths'])
                self.assertEqual(set(task['seed_files']) - set(baseline['seed_files']), {'sdk-reference.json'})
                self.assertEqual(task['contract'], baseline['contract'])
                self.assertNotIn(MARKER, json.dumps(task))
                self.assertEqual((output / 'inputs/reference.py').read_bytes(), (plain / 'inputs/reference.py').read_bytes())
        with self.assertRaisesRegex(ValueError, 'Conflicting sdk_reference'):
            self.cann_prepare('conflict', self.sdk, False)
        self.assertFalse((self.base / 'conflict').exists())

    def test_real_codegen_prepares_opt_in_tools_before_inert_host_start(self):
        # Evaluate the actual ES module. Only its session/UI dependencies are
        # replaced; execFile runs the real Python preparation and registry.
        script = r'''
import vm from 'node:vm';
import fs from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
const [source, base, python, sdk] = process.argv.slice(1);
const captures = [];
const context = vm.createContext({process, URL, structuredClone, console});
const stubs = {
 './host.mjs': {startApplication: async options => { captures.push(options); return {applicationSessionId: 'fixture', close() {}}; }, attachApplication() {}, sessions() {}},
 './session-picker.mjs': {pickSession() {}}, './project-info.mjs': {projectInfo() {}},
 './credentials.mjs': {loadCodegenCredentials() {}},
};
const module = new vm.SourceTextModule(await fs.readFile(source, 'utf8'), {context,
 initializeImportMeta(meta) { meta.url = pathToFileURL(source).href; }});
await module.link(async specifier => {
 const value = specifier.startsWith('node:') ? await import(specifier) : stubs[specifier];
 if (!value) throw new Error('Unexpected module: ' + specifier);
 return new vm.SyntheticModule(Object.keys(value), function() {
   for (const [key, item] of Object.entries(value)) this.setExport(key, item);
 }, {context});
});
await module.evaluate();
await fs.mkdir(base + '/project');
await fs.writeFile(base + '/expected.json', '{}');
for (const enabled of [false, true]) {
 const config = enabled ? {sdk_reference: JSON.parse(sdk)} : {};
 await module.namespace.startCodegen({python, expectedPythonModules: base + '/expected.json', output: base + '/' + enabled},
  {cwd: base + '/project', config, routes: ['fixture'], providers: {}, credits: null}, {});
}
console.log(JSON.stringify({captures, projectFiles: await fs.readdir(base + '/project')}));
'''
        output = self.base / 'codegen'; output.mkdir()
        result = subprocess.run([shutil.which('node'), '--experimental-vm-modules', '--input-type=module', '-e',
            script, str(ROOT / 'source/apps/agent/src/application/codegen.mjs'), str(output), sys.executable,
            json.dumps(self.sdk)], capture_output=True, text=True, timeout=30,
            env={**os.environ, 'KERNELBRIDGE_HOME': str(output / 'home'), 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads(result.stdout)
        self.assertEqual(recorded['projectFiles'], [])
        self.assertTrue(all(item['sessionOnly'] for item in recorded['captures']))
        plain = json.loads((output / 'false/config.json').read_text())
        self.assertNotIn('business_tools', plain)
        self.assertFalse((output / 'false/operator-tools-input.json').exists())
        enabled = json.loads((output / 'true/config.json').read_text())
        self.assert_sdk_tools(enabled['business_tools'])
        self.assertNotIn('sdk_reference', enabled)
        self.assertEqual(json.loads((output / 'true/task.json').read_text())['seed_files'], {})


class ChildAccessTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp
    tearDown = support.RuntimeTests.tearDown
    session = support.RuntimeTests.session

    def scenario(self, mode='read_only', kind='read', subset=False, configured=True):
        selection = sdk_fixture(self.base)
        prepared = sdk_prepare.prepare(selection, self.base / 'sdk-tools', sys.executable)
        tools = configured_tools([BusinessToolConfig.model_validate_json(json.dumps(row))
                                  for row in prepared['business_tools']], self.workspace, self.base / 'tool-state')
        if subset:
            tools = [tool for tool in tools if tool.spec.name == 'sdk_read']
        if not configured:
            tools = []
        delegate = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        delegate.add_agents([{'name': 'sdk-reader', 'tools': kind, 'description': 'Read admitted SDK reference'}])
        session, _, replay = self.session([('delegate', {'agent': 'sdk-reader', 'task': 'Read the installed interface declaration', 'mode': mode})], extra_tools=[delegate, *tools])
        child = Child([('sdk_read', {'resource': 'fixture', 'path': 'include/interface.h'})] if configured else [])
        session.child_proposers = {'primary': child}
        self.assertEqual(session.run(self.task)['status'], 'finished')
        replay._boundary()
        rows = list(self.store.snapshot()['actions'].values())
        return child, rows, session

    def assert_read_body(self, child, rows):
        reads = [row for row in rows if '/leaf/' in row['action']['id'] and row['action']['tool'] == 'sdk_read']
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]['outcome']['status'], 'completed', reads)
        self.assertIn(MARKER, json.dumps(reads[0]['outcome']))
        self.assertIn(MARKER, json.dumps(child.contexts[-1]))
        self.assertFalse((self.workspace.root / 'include/interface.h').exists())

    def test_read_child_gets_original_sdk_text_and_correct_pagination_guidance(self):
        child, rows, session = self.scenario()
        self.assert_read_body(child, rows)
        self.assertTrue(all(SDK_TOOLS <= names for names in child.tools))
        text = child.contexts[0]['instruction']
        self.assertIn('Workspace file tools use paths relative', text)
        self.assertIn('resource, run and read references', text)
        for marker in ('eof=true', 'next_read=null', 'next_offset=null for workspace text pages'):
            self.assertIn(marker, text)
        count = child.count
        session.run(self.task)
        self.assertEqual(child.count, count)

    def test_test_child_can_read_sdk_without_gaining_workspace_writes(self):
        child, rows, _ = self.scenario(kind='test')
        self.assert_read_body(child, rows)
        self.assertTrue(all(SDK_TOOLS | {'run_command'} <= names for names in child.tools))
        self.assertFalse(any('write_file' in names for names in child.tools))

    def test_child_receives_only_parent_admitted_sdk_subset(self):
        child, rows, _ = self.scenario(subset=True)
        self.assert_read_body(child, rows)
        self.assertTrue(all('sdk_read' in names and 'sdk_search' not in names for names in child.tools))

    def test_analysis_child_cannot_call_sdk_even_when_parent_registered_it(self):
        child, rows, _ = self.scenario(mode='analysis')
        self.assertEqual(child.tools, [set()])
        self.assertFalse(any('/leaf/' in row['action']['id'] and row['action']['tool'] == 'sdk_read' for row in rows))
        result = next(row['outcome'] for row in rows if row['action']['tool'] == 'delegate')
        self.assertEqual(result['data']['reason'], 'unexpected_tool')

    def test_unconfigured_parent_does_not_advertise_sdk_to_child(self):
        child, _, _ = self.scenario(configured=False)
        self.assertTrue(child.tools)
        self.assertFalse(any(SDK_TOOLS & names for names in child.tools))


if __name__ == '__main__':
    unittest.main()
