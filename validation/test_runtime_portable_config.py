"""Portable runtime configuration must not select a developer's deployment."""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'source/packages/executor/src/kernelcat_executor/evaluation/protocols/cannbench/native/r950_tools'
PACKAGE = '_kb_portable_runtime_ab_sdk'
CONTROL_PACKAGE = '_kb_portable_runtime_control'


def load_module(name: str, path: Path, *, package: bool = False):
    options = {'submodule_search_locations': [str(path.parent)]} if package else {}
    spec = importlib.util.spec_from_file_location(name, path, **options)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PortableRuntimeConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import under a private package name so other suites' SDK imports,
        # operator catalogs and the standard-library wave module stay untouched.
        with mock.patch.dict(os.environ, {}, clear=True):
            load_module(PACKAGE, TOOLS / 'ab_sdk/__init__.py', package=True)
            cls.config = importlib.import_module(f'{PACKAGE}.config')
            cls.transport = importlib.import_module(f'{PACKAGE}.ssh')
            cls.jobs = importlib.import_module(f'{PACKAGE}.job')
            cls.client = importlib.import_module(f'{PACKAGE}.client')
            cls.ktp = importlib.import_module(f'{PACKAGE}.ktp')
            cls.wave = load_module('_kb_portable_runtime_wave', TOOLS / 'wave.py')
            cls.layout = importlib.import_module(f'{PACKAGE}.layout')
            cls.hosts = importlib.import_module(f'{PACKAGE}.hosts')
            cls.host_backend = importlib.import_module(f'{PACKAGE}.demo1')
            load_module(CONTROL_PACKAGE, TOOLS.parent / 'control_plane/__init__.py', package=True)
            cls.execution = importlib.import_module(f'{CONTROL_PACKAGE}.execution')
            cls.control_cli = importlib.import_module(f'{CONTROL_PACKAGE}.cli')
            with mock.patch.dict(sys.modules, {'wave': cls.wave}), mock.patch.object(sys, 'path', sys.path.copy()):
                cls.percase = load_module('_kb_portable_runtime_percase', TOOLS / 'percase910.py')

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name in (PACKAGE, CONTROL_PACKAGE) or name.startswith((PACKAGE + '.', CONTROL_PACKAGE + '.')) or name in ('_kb_portable_runtime_wave', '_kb_portable_runtime_percase'):
                sys.modules.pop(name, None)

    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_unconfigured_target_never_starts_transport(self):
        cfg = self.config.Config()
        with mock.patch.object(self.transport, '_run') as run:
            for target in (None, 'demo1', 'demo2'):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    self.transport.ssh('printf fixture', cfg, host=target)
            run.assert_not_called()

    def test_explicit_profile_preserves_target_and_port(self):
        cfg = self.config.Config(
            demo1_host='alice@primary.example', demo1_port=2201, demo1_bench='/srv/bench',
            demo2_host='alice@secondary.example', demo2_port=2202, demo2_bench='/srv/other-bench',
        )
        response = self.transport.CmdResult(0, 'fixture', '')
        with mock.patch.object(self.transport, '_run', return_value=response) as run:
            for target in ('demo2', 'alice@secondary.example'):
                with self.subTest(target=target):
                    self.assertEqual(self.transport.ssh('printf fixture', cfg, host=target).stdout, 'fixture')
                    argv = run.call_args.args[0]
                    self.assertEqual(argv[0], 'ssh')
                    self.assertEqual(argv[argv.index('-p') + 1], '2202')
                    self.assertIn('alice@secondary.example', argv)
                    self.assertNotIn('StrictHostKeyChecking=no', argv)

    def test_local_host_marker_does_not_redirect_another_host(self):
        cfg = self.config.Config(demo1_host='alice@primary.example', demo2_host='alice@secondary.example')
        response = self.transport.CmdResult(0, 'fixture', '')
        with mock.patch.dict(os.environ, {'KB_AB_LOCAL_HOST': 'demo1'}):
            with mock.patch.object(self.transport, '_run', return_value=response) as run:
                self.transport.ssh('printf fixture', cfg, host='demo1')
                self.assertEqual(run.call_args.args[0], ['bash', '-lc', 'printf fixture'])
                self.transport.ssh('printf fixture', cfg, host='demo2')
                self.assertEqual(run.call_args.args[0][0], 'ssh')

    def test_explicit_local_mode_needs_no_ssh_target(self):
        with mock.patch.dict(os.environ, {'AB_LOCAL': '1'}):
            with mock.patch.object(self.transport, '_run', return_value=self.transport.CmdResult(0, 'ok', '')) as run:
                self.transport.ssh('printf ok', self.config.Config())
                self.assertEqual(run.call_args.args[0], ['bash', '-lc', 'printf ok'])

    def test_absent_optional_catalog_accepts_canonical_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.multiple(self.jobs, CANON_PATH=Path(temporary) / 'absent.json', _CANON=None):
                self.assertEqual(self.jobs.canon_op('GenericReduction'), 'GenericReduction')
                with self.assertRaises(KeyError):
                    self.jobs.canon_op('unknown_snake_name')

    def test_configured_catalog_resolves_alias_and_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog = Path(temporary) / 'catalog.json'
            catalog.write_text(json.dumps({'customreduce': 'CustomReduce'}))
            with mock.patch.dict(os.environ, {'KB_AB_OPERATOR_CATALOG': str(catalog)}):
                with mock.patch.multiple(self.jobs, CANON_PATH=catalog, _CANON=None):
                    self.assertEqual(self.jobs.canon_op('custom_reduce'), 'CustomReduce')
                catalog.unlink()
                with mock.patch.multiple(self.jobs, CANON_PATH=catalog, _CANON=None):
                    with self.assertRaises(FileNotFoundError):
                        self.jobs.canon_op('CustomReduce')

    def test_unconfigured_backend_reports_no_available_capacity(self):
        cfg = self.config.Config()
        client = self.client.AbClient(cfg)
        self.assertEqual(client.layout()['total_logical_npu'], 0)
        with mock.patch.object(self.transport, '_run') as run:
            with self.assertRaises(ValueError):
                client.pick_backend()
            run.assert_not_called()

    def test_missing_ktp_image_rejected_before_sync_or_claim(self):
        backend = self.ktp.KtpBackend(self.config.Config(ktp_image=''))
        with mock.patch.object(backend, '_prepare') as prepare:
            with mock.patch.object(self.ktp, '_acquire_ab_claim') as claim:
                with self.assertRaises(ValueError):
                    backend.submit_ab(object())
                prepare.assert_not_called()
                claim.assert_not_called()

    def test_empty_secondary_does_not_fall_back_to_primary(self):
        cfg = self.config.Config(demo1_host='primary.example', demo2_host='', demo2_bench='/srv/bench',
                                 extra_env={'CANN_REBUILD_PACK': '/srv/package.tgz'})
        with mock.patch.object(self.transport, '_run') as run:
            for operation in (
                lambda: self.transport.ssh('true', cfg, host=''),
                lambda: self.transport.ssh('true', cfg, host='demo2'),
                lambda: self.hosts.probe_host('demo2', cfg),
                lambda: self.hosts.rebuild_eval_env('demo2', cfg),
            ):
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    operation()
            run.assert_not_called()

    def test_bare_secondary_alias_keeps_secondary_port(self):
        cfg = self.config.Config(demo1_host='primary-alias', demo1_port=2201,
                                 demo2_host='secondary-alias', demo2_port=2202)
        response = self.transport.CmdResult(0, 'ok', '')
        with mock.patch.object(self.transport, '_run', return_value=response) as run:
            self.transport.ssh('true', cfg, host=cfg.host('demo2').ssh)
            argv = run.call_args.args[0]
            self.assertIn('secondary-alias', argv)
            self.assertEqual(argv[argv.index('-p') + 1], '2202')

    def test_ab_missing_bench_fails_before_remote_preflight(self):
        cfg = self.config.Config(demo1_host='primary.example', demo1_bench='')
        backend = self.host_backend.HostBackend(cfg=cfg)
        spec = mock.Mock(devices=(0,), arm_specs=mock.Mock(return_value=['candidate:/fixture']),
                         op='GenericReduction', reps=1)
        with mock.patch.object(self.host_backend, 'ssh') as ssh:
            with mock.patch.object(self.host_backend.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    backend.submit_ab(spec)
                ssh.assert_not_called()
                run.assert_not_called()

    def test_fleet_does_not_drop_operators_when_only_ktp_is_configured(self):
        cfg = self.config.Config(ktp_image='registry.example/npu:fixture')
        plan = self.layout.fill_fleet(2, cfg)
        self.assertEqual(plan['host_slots'], 0)
        self.assertEqual(plan['hosts'], [])
        self.assertEqual(plan['ktp_jobs'], 2)
        self.assertEqual(len(plan['ktp']['jobs']), 2)
        cfg = self.config.Config(demo2_host='secondary.example', demo2_bench='/srv/bench',
                                 ktp_image='registry.example/npu:fixture')
        plan = self.layout.fill_fleet(2, cfg)
        self.assertEqual(plan['hosts'][0]['backend'], 'demo2')
        self.assertEqual(plan['host_slots'] + plan['ktp_jobs'], 2)

    def test_runtime_host_helpers_support_python_311_syntax(self):
        for name in ('hosts.py', 'ssh.py', 'config.py', 'layout.py', 'demo1.py'):
            path = TOOLS / 'ab_sdk' / name
            with self.subTest(module=name):
                ast.parse(path.read_text(), filename=str(path), feature_version=(3, 11))

    def test_control_plane_profiler_passes_explicit_or_environment_bench(self):
        adapter = self.execution.LegacyToolAdapter(TOOLS.parent.parent)
        with tempfile.TemporaryDirectory() as temporary:
            fields = dict(base_source=temporary, candidate_source=temporary,
                          artifact_dir=Path(temporary) / 'artifacts', run_id='fixture',
                          device=0, case_id='1', school_env='/fixture/profile.env')
            with self.assertRaises(self.execution.ContractError):
                adapter.profiler(**fields)
            with mock.patch.dict(os.environ, {'CANN_BENCH_HOME': '/remote/bench with spaces'}):
                argv = adapter.profiler(**fields).argv
                self.assertEqual(argv[argv.index('--bench') + 1], '/remote/bench with spaces')
                argv = adapter.profiler(**fields, bench='/explicit/bench').argv
                self.assertEqual(argv[argv.index('--bench') + 1], '/explicit/bench')
                args = self.control_cli.build_parser().parse_args([
                    'shadow-profiler', '--repo-root', str(TOOLS.parent.parent),
                    '--base', temporary, '--candidate', temporary,
                    '--artifact-dir', str(Path(temporary) / 'output'), '--run-id', 'fixture',
                    '--device', '0', '--case-id', '1', '--bench', '/cli/bench',
                ])
                argv = args.func(args)['plan']['argv']
                self.assertEqual(argv[argv.index('--bench') + 1], '/cli/bench')
            self.assertFalse((Path(temporary) / 'artifacts').exists())
            self.assertFalse((Path(temporary) / 'output').exists())

    def test_percase_identity_is_validated_before_api(self):
        with mock.patch.object(self.wave, 'api') as api:
            for environment in ({'GITCODE_TOKEN': 'fixture'}, {'CANN_US': 'fixture-user'}, {}):
                with self.subTest(environment=list(environment)), mock.patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValueError):
                        self.percase.oracle()
            with mock.patch.dict(os.environ, {'GITCODE_TOKEN': 'fixture'}), mock.patch.object(self.wave, 'SOL', ''):
                with self.assertRaises(ValueError):
                    self.percase.collect()
            api.assert_not_called()

    def test_submission_wave_checks_identity_before_loading_or_dispatching(self):
        for missing in ('SOL', 'TAG', 'AGG'):
            identities = dict(SOL='fixture-solution', TAG='fixture-tag', AGG='fixture-aggregation')
            identities[missing] = ''
            with self.subTest(missing=missing), mock.patch.multiple(self.wave, **identities):
                with mock.patch.object(self.wave, 'api') as api, mock.patch.object(self.wave, 'jload') as load:
                    with self.assertRaises(ValueError):
                        self.wave.cmd_run()
                    load.assert_not_called()
                    api.assert_not_called()

    def test_auto_ab_rejects_missing_bench_without_ssh(self):
        cfg = self.config.Config(demo1_host='primary.example', demo1_bench='',
                                 demo2_host='secondary.example', demo2_bench='')
        client = self.client.AbClient(cfg)
        with mock.patch.object(self.transport, '_run') as run:
            with self.assertRaises(ValueError):
                client.ab(object(), backend='auto')
            run.assert_not_called()

    def test_local_auto_ab_traverses_host_preflight_and_local_driver(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'source/op_kernel').mkdir(parents=True)
            driver = folder / 'local-driver.sh'
            driver.write_text(
                'test "$AB_LOCAL" = "1" || exit 11\n'
                'test -z "$AB_SSH_HOST" || exit 12\n'
                'test -n "$CANN_BENCH_HOME" || exit 13\n'
                'printf "LOCAL_FIXTURE_OK\\n"\n'
            )
            cfg = self.config.Config(demo1_host='must-not-use-primary.example', demo1_bench='',
                                     demo2_host='', demo2_bench=str(folder / 'bench'))
            client = self.client.AbClient(cfg)
            spec = self.jobs.AbSpec(op='GenericReduction', reps=1,
                                   arms={'candidate': str(folder / 'source')})
            observed = []

            def observe_local_preflight(argv, **kwargs):
                # Stabilize the process inventory; the full client -> backend ->
                # SSH adapter path still chooses the actual local command.
                self.assertEqual(argv[:2], ['bash', '-lc'])
                observed.append(argv)
                return self.transport.CmdResult(0, '0\n', '')

            with mock.patch.dict(os.environ, {'AB_LOCAL': '1'}):
                with mock.patch.object(self.host_backend, 'AB910', driver):
                    with mock.patch.object(self.transport, '_run', side_effect=observe_local_preflight):
                        result = client.ab(spec, backend='auto', devices=(0,), detach=False)
            self.assertTrue(observed)
            self.assertEqual(result.backend, 'demo2')
            self.assertEqual(result.extra['rc'], 0)
            self.assertIn('LOCAL_FIXTURE_OK', result.extra['stdout_tail'])

    def test_missing_submission_identity_never_calls_api(self):
        for tag, aggregation in (('', ''), ('configured-tag', ''), ('', 'configured-aggregation')):
            with self.subTest(tag=bool(tag), aggregation=bool(aggregation)):
                with mock.patch.multiple(self.wave, TAG=tag, AGG=aggregation):
                    with mock.patch.object(self.wave, 'api') as api:
                        with self.assertRaises(ValueError):
                            self.wave.submit_zip('/nonexistent-package.zip', ['GenericReduction'], 'fixture-token')
                        api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
