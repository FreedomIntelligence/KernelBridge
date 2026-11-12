"""Real local fixture subprocesses; no CANN compiler, model, or NPU execution."""
from contextlib import ExitStack, nullcontext, redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
sys.path.insert(0, str(ROOT / 'examples/scatter'))
from kernelcat_executor.backends.ascend import compiler, runtime
from kernelcat_executor.build.inputs import BuildSettings

DRIVERS = tuple(importlib.import_module(name) for name in ('npu_benchmark', 'npu_matmul_benchmark'))
TARGET = 'ai_core-Ascend910_9382'
SOURCES = {name: '// fixture source; never compiled as C++\n' for name in compiler._FIELDS}
SOURCES['project_json_src'] = '[{"op": "StatelessCustom"}]'
SOURCES['model_src'] = '# fixture wrapper; never evaluated\n'


class NativeBuildDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kb-native-build-receipts-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / 'fake-sdk-bin'
        self.bin.mkdir()
        installer = f'#!{sys.executable}\n' + '''import sys
from pathlib import Path
root = Path(next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--install-path=')))
folder = root / 'vendors/customize'
folder.mkdir(parents=True)
(folder / 'fixture.so').write_bytes(b'fixture artifact, not a native library')
print('fixture install complete')
'''
        builder = f'#!{sys.executable}\n' + '''import os, sys
from pathlib import Path
print(os.environ.get('KB_FAKE_BUILD_LINES', ''), flush=True)
code = int(os.environ.get('KB_FAKE_BUILD_RC', '0'))
if code:
    raise SystemExit(code)
folder = Path('build_out')
folder.mkdir()
(folder / 'no_payload_tiling_data.h').write_bytes(b'')
''' + f"installer = folder / 'custom_opp_fixture.run'\ninstaller.write_text({installer!r})\ninstaller.chmod(0o755)\n"
        extension = "from pathlib import Path\nPath('custom_ops_lib.fixture.so').write_bytes(b'fixture extension, never loaded')\n"
        generator = f'#!{sys.executable}\n' + r'''import sys
from pathlib import Path
project = Path(sys.argv[sys.argv.index('-out') + 1])
name = Path(sys.argv[sys.argv.index('-i') + 1]).stem
(project / 'op_host').mkdir(parents=True)
(project / 'op_kernel').mkdir()
(project / 'op_kernel' / (name + '_tiling.h')).write_text('// placeholder\n')
(project / 'op_kernel/CMakeLists.txt').write_text('add_kernels_compile()\n')
''' + f"script = project / 'build.sh'\nscript.write_text({builder!r})\nscript.chmod(0o755)\n" + f"Path('CppExtension/setup.py').write_text({extension!r})\n"
        command = self.bin / 'msopgen'
        command.write_text(generator)
        command.chmod(0o755)
        self.environment = {'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                            'PYTHONDONTWRITEBYTECODE': '1', 'KB_FAKE_BUILD_LINES': '', 'KB_FAKE_BUILD_RC': '0'}
        self.counter = 0

    def settings(self):
        self.counter += 1
        workspace = self.root / f'job-{self.counter}' / 'build'
        (workspace / 'CppExtension/csrc').mkdir(parents=True)
        return BuildSettings('stateless', workspace, TARGET, 0)

    def compile(self, settings, **environment):
        with patch.dict(os.environ, {**self.environment, **environment}):
            return compiler.compile_sources(SOURCES, settings)

    def assert_manifest(self, report, settings):
        self.assertEqual(report['schema_version'], 'kernelbridge.native-build-diagnostics/v1')
        self.assertEqual(report['source_digest'], compiler._canonical_digest(
            {key: SOURCES[key] for key in compiler._FIELDS}))
        self.assertEqual(report['target'], TARGET)
        for name, sha in report['artifacts'].items():
            self.assertTrue(name.startswith('build/'))
            self.assertEqual(hashlib.sha256((settings.workspace.parent / name).read_bytes()).hexdigest(), sha)
        for step in report['steps']:
            self.assertIsInstance(step['started_at_ns'], str)
            self.assertTrue(step['started_at_ns'].isdigit())
            for key in ('metadata', 'log'):
                record = step[key]
                self.assertEqual(report['artifacts'][record['path']], record['sha256'])
                self.assertEqual((settings.workspace.parent / record['path']).stat().st_size, record['bytes'])

    def test_zero_exit_sdk_errors_are_visible_without_overriding_real_build_success(self):
        for marker in ('[ERROR] SDK fixture diagnostic', '[E1112 12:34:56] SDK fixture diagnostic',
                       'Execution_Error(EZ1008): fixture diagnostic'):
            with self.subTest(marker=marker):
                settings = self.settings()
                fake_result = json.dumps({'status': 'completed', 'quoted': '[ERROR] quoted final result'})
                outputs = self.compile(settings, KB_FAKE_BUILD_LINES=fake_result + '\n' + marker)
                self.assertTrue(outputs.files)
                report = compiler.collect_build_diagnostics(settings.workspace)
                self.assert_manifest(report, settings)
                self.assertEqual(report['status'], 'completed')
                self.assertEqual([step['returncode'] for step in report['steps']], [0, 0, 0, 0])
                step = next(step for step in report['steps'] if step['argv'] == ['./build.sh'])
                self.assertEqual(step['first_sdk_diagnostic'],
                                 {'text': marker, 'line': 3, 'path': step['log']['path']})

    def test_stateless_non_scatter_empty_generated_header_is_not_a_build_failure(self):
        settings = self.settings()
        outputs = self.compile(settings)
        report = compiler.collect_build_diagnostics(settings.workspace)
        self.assertEqual(outputs.source_digest, report['source_digest'])
        self.assertEqual(report['status'], 'completed')
        self.assertEqual((settings.workspace / 'StatelessCustom/build_out/no_payload_tiling_data.h').stat().st_size, 0)
        self.assertFalse(any(step.get('first_sdk_diagnostic') for step in report['steps']))

    def test_nonzero_exit_retains_real_code_and_verified_failure_log(self):
        settings = self.settings()
        with self.assertRaisesRegex(RuntimeError, 'Exit Code: 7'):
            self.compile(settings, KB_FAKE_BUILD_RC='7', KB_FAKE_BUILD_LINES='[ERROR] original fixture build error')
        report = compiler.collect_build_diagnostics(settings.workspace)
        self.assert_manifest(report, settings)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual([step['returncode'] for step in report['steps']], [0, 7])
        self.assertEqual(report['steps'][-1]['first_sdk_diagnostic']['text'], '[ERROR] original fixture build error')

    def test_launch_exception_does_not_fabricate_a_return_code(self):
        settings = self.settings()
        with self.assertRaises(FileNotFoundError):
            self.compile(settings, PATH=str(self.root / 'no-executables'))
        report = compiler.collect_build_diagnostics(settings.workspace)
        self.assert_manifest(report, settings)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(len(report['steps']), 1)
        self.assertIsNone(report['steps'][0]['returncode'])
        self.assertIn('FileNotFoundError', report['steps'][0]['error'])

    def test_step_metadata_log_and_binding_tampering_are_not_accepted(self):
        settings = self.settings()
        self.compile(settings)
        original = compiler.collect_build_diagnostics(settings.workspace)
        step = original['steps'][1]
        metadata = settings.workspace.parent / step['metadata']['path']
        log = settings.workspace.parent / step['log']['path']
        original_metadata, original_log = metadata.read_bytes(), log.read_bytes()
        mutations = {
            'source': lambda payload: payload['binding'].update(source_digest='0' * 64),
            'target': lambda payload: payload['binding'].update(target='ai_core-Ascend910B'),
            'path': lambda payload: payload['log'].update(path='build/forged.log'),
            'log_hash': lambda payload: payload['log'].update(sha256='0' * 64),
            'log_size': lambda payload: payload['log'].update(bytes=1),
            'cwd': lambda payload: payload.update(cwd=str(self.root)),
            'returncode': lambda payload: payload.update(returncode=True),
        }
        for name in (*mutations, 'metadata_hash', 'log_bytes'):
            with self.subTest(mutation=name):
                metadata.write_bytes(original_metadata)
                log.write_bytes(original_log)
                if name == 'log_bytes':
                    log.write_bytes(original_log + b'changed original log\n')
                else:
                    envelope = json.loads(original_metadata)
                    if name == 'metadata_hash':
                        envelope['payload']['returncode'] = 17
                    else:
                        mutations[name](envelope['payload'])
                        envelope['payload_sha256'] = compiler._canonical_digest(envelope['payload'])
                    metadata.write_text(json.dumps(envelope))
                report = compiler.collect_build_diagnostics(settings.workspace)
                self.assertEqual(report['status'], 'partial')
                self.assertEqual(len(report['steps']), 3)
                self.assertNotIn(step['metadata']['path'], report['artifacts'])
                self.assertNotIn(step['log']['path'], report['artifacts'])
                self.assertTrue(report['issues'])

    def test_missing_log_or_sidecar_never_implies_a_complete_collection(self):
        settings = self.settings()
        self.compile(settings)
        report = compiler.collect_build_diagnostics(settings.workspace)
        metadata = settings.workspace.parent / report['steps'][1]['metadata']['path']
        log = settings.workspace.parent / report['steps'][1]['log']['path']
        for removed in (metadata, log):
            with self.subTest(removed=removed.name):
                original = removed.read_bytes()
                removed.unlink()
                observed = compiler.collect_build_diagnostics(settings.workspace)
                self.assertEqual(observed['status'], 'partial')
                self.assertEqual(len(observed['steps']), 3)
                self.assertTrue(observed['issues'])
                removed.write_bytes(original)

    def test_binding_hash_failure_cannot_expose_verified_steps(self):
        settings = self.settings()
        self.compile(settings)
        path = settings.workspace / '.kernelbridge-build-binding.json'
        envelope = json.loads(path.read_text())
        envelope['payload']['target'] = 'ai_core-Ascend910B'
        path.write_text(json.dumps(envelope))
        report = compiler.collect_build_diagnostics(settings.workspace)
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['steps'], [])
        self.assertEqual(report['artifacts'], {})

    def test_internal_file_and_directory_links_are_rejected_but_root_alias_is_allowed(self):
        settings = self.settings()
        self.compile(settings)
        original = compiler.collect_build_diagnostics(settings.workspace)
        alias = self.root / 'workspace-alias'
        alias.symlink_to(settings.workspace, target_is_directory=True)
        self.assertEqual(compiler.collect_build_diagnostics(alias), original)
        step = original['steps'][1]
        log = settings.workspace.parent / step['log']['path']
        backup = log.with_suffix('.saved')
        shutil.copyfile(log, backup)
        log.unlink()
        log.symlink_to(backup)
        report = compiler.collect_build_diagnostics(settings.workspace)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(any('symbolic link' in issue['reason'] for issue in report['issues']))
        linked = settings.workspace / 'nested-alias'
        linked.symlink_to(backup.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            compiler._artifact(linked / backup.name, settings.workspace.parent, workspace=settings.workspace)

    def test_build_through_bound_workspace_root_alias_preserves_output_identity(self):
        canonical = self.settings()
        alias = self.root / 'build-root-alias'
        alias.symlink_to(canonical.workspace, target_is_directory=True)
        settings = BuildSettings('stateless', alias, TARGET, 0)
        outputs = self.compile(settings)
        self.assertEqual(outputs.workspace, alias)
        report = compiler.collect_build_diagnostics(alias)
        self.assertEqual(report['status'], 'completed')
        self.assert_manifest(report, canonical)

    def run_driver(self, driver, stage, *, fail_collection=False):
        self.counter += 1
        candidate = self.root / f'candidate-{self.counter}.py'
        candidate.write_text(''.join(f'{key} = {value!r}\n' for key, value in SOURCES.items()))
        is_cube = driver.__name__ == 'npu_matmul_benchmark'
        args = SimpleNamespace(candidate=candidate, output=self.root / f'driver-job-{self.counter}',
                               mode='matmul' if is_cube else 'max', soc=TARGET, device=1, warmup=3,
                               repeats=20 if is_cube else 10, pairs=12 if is_cube else 3, profile=False)
        class Model:
            def eval(self): return self
            def to(self, device): return self
            def __call__(self, *args): raise RuntimeError('original runtime failure')
        instance = SimpleNamespace(device='npu:1', context={'ModelNew': Model}, load=Mock(),
                                   close=Mock(side_effect=RuntimeError('secondary cleanup failure')))
        if stage == 'load': instance.load.side_effect = RuntimeError('original load failure')
        torch = ModuleType('torch')
        torch.__version__ = 'fixture-only'
        torch.no_grad = nullcontext
        torch.equal = lambda left, right: left == right
        torch.npu = SimpleNamespace(synchronize=lambda: None, get_device_name=lambda _: 'fixture only')
        torch_npu = ModuleType('torch_npu')
        torch_npu.__version__ = 'fixture-only'
        cases = [{'id': 'fixture', 'seed': 0, 'input_sha256': 'fixture', 'src': None, 'index': None,
                  'a': None, 'b': None, 'expected': 'oracle'}]
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {**self.environment,
                'KB_FAKE_BUILD_RC': '7' if stage == 'compile' else '0',
                'KB_FAKE_BUILD_LINES': '[ERROR] fixture build diagnostic'}))
            stack.enter_context(patch.dict(sys.modules, {'torch': torch, 'torch_npu': torch_npu}))
            stack.enter_context(patch.object(runtime, 'AscendRuntime', return_value=instance))
            stack.enter_context(patch.object(driver, 'collect_ir', return_value=[]))
            stack.enter_context(patch.object(driver, 'validated_ir_artifacts', return_value={}))
            stack.enter_context(patch.object(driver, 'suite', return_value=cases))
            if fail_collection:
                stack.enter_context(patch.object(compiler, 'collect_build_diagnostics', side_effect=OSError('receipt unreadable')))
            if is_cube:
                stack.enter_context(patch.object(driver, 'audit_sources', return_value={}))
                stack.enter_context(patch.object(driver, 'check', side_effect=lambda model, *unused: model(None, None)))
            else:
                stack.enter_context(patch.object(driver, 'audit_wrapper', return_value={}))
                stack.enter_context(patch.object(driver, 'audit_native_binding', return_value={}))
                stack.enter_context(patch.object(driver, 'scatter_eager', return_value=SimpleNamespace(cpu=lambda: 'oracle')))
                stack.enter_context(patch.object(driver, 'measure', return_value=1))
            with redirect_stdout(io.StringIO()):
                self.assertFalse(driver.run(args))
        return json.loads((args.output / 'result.json').read_text()), args

    def test_both_benchmarks_keep_verified_build_manifest_after_load_or_runtime_failure(self):
        for driver in DRIVERS:
            for stage in ('load', 'runtime'):
                with self.subTest(driver=driver.__name__, stage=stage):
                    result, args = self.run_driver(driver, stage)
                    self.assertTrue(result['build_completed'])
                    self.assertEqual(result['message'], f'original {stage} failure')
                    self.assertEqual(result['cleanup_error'], 'secondary cleanup failure')
                    manifest = result['build_diagnostics']
                    self.assertEqual(manifest['status'], 'completed')
                    self.assertEqual(manifest['target'], result['soc'])
                    self.assertEqual(manifest['source_digest'], result['build_source_digest'])
                    self.assertEqual(manifest['candidate_sha256'], hashlib.sha256(args.candidate.read_bytes()).hexdigest())
                    self.assertEqual([step['returncode'] for step in manifest['steps']], [0, 0, 0, 0])
                    self.assertTrue(any(step.get('first_sdk_diagnostic') for step in manifest['steps']))

    def test_both_benchmarks_keep_failed_compile_receipt_without_marking_build_complete(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                result, _ = self.run_driver(driver, 'compile')
                self.assertFalse(result['build_completed'])
                self.assertEqual(result['failure_stage'], 'compile')
                self.assertIn('Exit Code: 7', result['message'])
                self.assertEqual([step['returncode'] for step in result['build_diagnostics']['steps']], [0, 7])

    def test_collection_failure_never_masks_original_runtime_or_cleanup_error(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                result, _ = self.run_driver(driver, 'load', fail_collection=True)
                self.assertTrue(result['build_completed'])
                self.assertEqual(result['message'], 'original load failure')
                self.assertEqual(result['cleanup_error'], 'secondary cleanup failure')
                self.assertEqual(result['build_diagnostics']['status'], 'unavailable')
                self.assertIn('receipt unreadable', result['build_diagnostics']['reason'])


if __name__ == '__main__':
    unittest.main()
