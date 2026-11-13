"""Installed-reference access, original-byte recovery, and real Journal projection."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for source in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(source))

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.contracts.models import Action, Conflict, Policy, Task, canonical
from kernelcat_executor.contracts.text import fit_observation_page
from kernelcat_executor.operations.context import project
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_worker.business_tools import configured_tools
from kernelcat_worker.configuration import BusinessToolConfig


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'examples/sdk_reference' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


snapshot = module('sdk_test_snapshot', 'snapshot.py')
driver = module('sdk_test_driver', 'driver.py')
prepare = module('sdk_test_prepare', 'prepare.py').prepare


def decoded(value):
    return value['text'].encode('utf-8') if value['encoding'] == 'utf-8' else base64.b64decode(value['text'], validate=True)


class SDKReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-sdk-reference-')
        self.base = Path(self.temp.name).resolve()
        self.sdk = self.base / 'installed-sdk'
        (self.sdk / 'include').mkdir(parents=True)
        self.source = ('// 当前 SDK 声明 "quoted" \\ marker\nint OriginalSDKDeclaration();\n' * 1200).encode()
        (self.sdk / 'include/api.h').write_bytes(self.source)
        self.manifest = snapshot.export_snapshot(self.sdk, self.base / 'snapshot', name='vendor',
            version='1.2', target='fixture', include=['include'])
        self.prepared = prepare({'resources': [{'id': 'vendor', 'manifest': str(self.base / 'snapshot/manifest.json')}]},
                                self.base / 'tools', sys.executable)
        self.config = json.loads((self.base / 'tools/config.json').read_text())
        self.calls = 0

    def tearDown(self):
        # Exported corpus directories are deliberately read-only.
        for path in self.base.rglob('*'):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        self.temp.cleanup()

    def invoke(self, tool='sdk_read', arguments=None, identity=None):
        self.calls += 1
        return driver.main(self.config, {'id': identity or 'call-' + str(self.calls),
            'tool': tool, 'arguments': {} if arguments is None else arguments})

    def json_result(self, result):
        self.assertEqual(result['status'], 'completed', result)
        value, raw = result['data'], b''
        for _ in range(100):
            raw += decoded(value)
            if value['eof']:
                return json.loads(raw)
            value = self.invoke(arguments=value['next_read']['arguments'])['data']
        self.fail('artifact continuation did not terminate')

    def changed_file(self, data=b'altered declaration\n'):
        file = self.base / 'snapshot/files/include/api.h'
        file.chmod(0o600)
        file.write_bytes(data)
        return file

    def install_raw_fixture(self, raw):
        self.source = raw
        (self.sdk / 'include/api.h').write_bytes(raw)
        self.manifest = snapshot.export_snapshot(self.sdk, self.base / 'raw-snapshot', name='vendor',
            version='1.2', target='fixture', include=['include'])
        self.prepared = prepare({'resources': [{'id': 'vendor', 'manifest': str(self.base / 'raw-snapshot/manifest.json')}]},
                                self.base / 'raw-tools', sys.executable)
        self.config = json.loads((self.base / 'raw-tools/config.json').read_text())

    def gateway(self):
        workspace = Workspace(self.base / 'workspace')
        store = AgentJournal(self.base / 'journal/events.jsonl')
        store.initialize(500, Policy(max_steps=100, context_bytes=262144, observation_bytes=8192))
        task = Task(id='sdk-task', scope='sdk-scope', goal='Inspect the selected SDK declaration.')
        store.register_task(task.model_dump(mode='json'))
        specs = [BusinessToolConfig.model_validate_json(json.dumps(row)) for row in self.prepared['business_tools']]
        tools = configured_tools(specs, workspace, self.base / 'tool-state')
        return Gateway(store, tools, task.scope), store, task

    def test_discovery_provides_selected_version_without_workspace_copy(self):
        result = self.json_result(self.invoke())
        resource = result['resources'][0]
        self.assertEqual((resource['resource'], resource['version'], resource['target']), ('vendor', '1.2', 'fixture'))
        self.assertEqual(resource['snapshot_id'], self.manifest['snapshot_id'])
        self.assertFalse((self.base / 'tools/files').exists())
        self.assertEqual(set(self.config['access'].values()) - {False},
                         {'manifest-listed regular NUL-free source bytes with matching size and SHA256'})

    def test_unregistered_resource_path_escape_and_unknown_parameters_are_rejected(self):
        for arguments in ({'resource': 'other', 'path': 'include/api.h'},
                          {'resource': 'vendor', 'path': '../installed-sdk/include/api.h'},
                          {'resource': 'vendor', 'path': '/etc/passwd'},
                          {'resource': 'vendor', 'path': 'include/not-registered.h'},
                          {'resource': 'vendor', 'path': 'include/api.h', 'host': 'unauthorized'}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(arguments=arguments)['status'], 'failed')

    def test_snapshot_symlink_substitution_is_not_followed_even_for_identical_bytes(self):
        file = self.base / 'snapshot/files/include/api.h'
        file.parent.chmod(0o700)
        file.unlink()
        file.symlink_to(self.sdk / 'include/api.h')
        self.assertEqual(self.invoke(arguments={'resource': 'vendor', 'path': 'include/api.h'})['status'], 'failed')
        self.assertEqual(self.invoke('sdk_search', {'resource': 'vendor', 'mode': 'path', 'query': 'api.h'})['status'], 'failed')

    def test_snapshot_parent_directory_symlink_substitution_is_rejected(self):
        directory = self.base / 'snapshot/files/include'
        directory.parent.chmod(0o700)
        directory.chmod(0o700)
        directory.rename(directory.with_name('original'))
        directory.symlink_to(self.sdk / 'include', target_is_directory=True)
        self.assertEqual(self.invoke(arguments={'resource': 'vendor', 'path': 'include/api.h'})['status'], 'failed')

    def test_source_read_and_content_search_reject_hash_drift(self):
        self.changed_file()
        for tool, arguments in [('sdk_read', {'resource': 'vendor', 'path': 'include/api.h'}),
                                ('sdk_search', {'resource': 'vendor', 'query': 'altered'})]:
            with self.subTest(tool=tool):
                result = self.invoke(tool, arguments)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('snapshot_file_changed', result['data']['reason'])

    def test_copied_manifest_drift_and_rehashed_wrong_snapshot_identity_are_rejected(self):
        resource = self.config['resources'][0]
        path = Path(resource['manifest'])
        original = path.read_bytes()
        path.chmod(0o600)
        changed = json.loads(original)
        changed['version'] = 'other-sdk'
        path.write_bytes(driver.canonical(changed))
        self.assertEqual(self.invoke()['status'], 'failed')
        # Even a caller bypassing the outer pin cannot relabel the body without
        # recomputing its complete, version-bearing snapshot identity.
        with self.assertRaisesRegex(driver.Rejected, 'snapshot_identity_changed'):
            driver.validate_manifest(path.read_bytes())

    def test_same_action_replays_original_result_after_corpus_changes(self):
        arguments = {'resource': 'vendor', 'path': 'include/api.h', 'limit': 100}
        original = self.invoke(arguments=arguments, identity='same-action')
        self.changed_file()
        with patch.object(driver, 'file_bytes', side_effect=AssertionError('must not rescan')):
            replay = self.invoke(arguments=arguments, identity='same-action')
        self.assertEqual(replay, original)
        with self.assertRaisesRegex(driver.Rejected, 'different_input'):
            self.invoke(arguments={**arguments, 'offset': 1}, identity='same-action')

    def test_retained_search_page_recovers_after_corpus_is_unavailable(self):
        original = self.invoke('sdk_search', {'resource': 'vendor', 'query': 'OriginalSDKDeclaration', 'limit': 2})
        report = self.json_result(original)
        run = original['data']['run']
        self.changed_file()
        with patch.object(driver, 'load_resource', side_effect=AssertionError('recovery must not inspect corpus')):
            recovered = self.json_result(self.invoke(arguments={'run': run, 'path': 'search.json'}))
        self.assertEqual(recovered, report)
        self.assertEqual(report['matches'][0]['sha256'], hashlib.sha256(self.source).hexdigest())

    def test_retained_receipt_artifact_tampering_is_rejected(self):
        result = self.invoke(arguments={'resource': 'vendor', 'path': 'include/api.h', 'limit': 100})
        run = result['data']['run']
        artifact = Path(self.config['state']) / run / 'page.bin'
        artifact.write_bytes(b'forged')
        with self.assertRaisesRegex(driver.Rejected, 'artifact_changed'):
            self.invoke(arguments={'run': run, 'path': 'page.bin'})

    def test_incomplete_receipt_is_unknown_and_never_recreated(self):
        identity = 'interrupted-before-request'
        root = Path(self.config['state'])
        folder = root / driver.sha(identity.encode())
        folder.mkdir(parents=True)
        with patch.object(driver, 'file_bytes', side_effect=AssertionError('must not retry')):
            result = self.invoke(arguments={'resource': 'vendor', 'path': 'include/api.h'}, identity=identity)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(list(folder.iterdir()), [])

    def test_misaligned_and_tiny_utf8_pages_preserve_exact_bytes(self):
        chinese = self.source.index('当'.encode())
        for offset, limit in [(chinese + 1, 8), (chinese, 1), (0, chinese + 1)]:
            with self.subTest(offset=offset, limit=limit):
                result = self.invoke(arguments={'resource': 'vendor', 'path': 'include/api.h', 'offset': offset, 'limit': limit})
                value = result['data']
                fitted = fit_observation_page(value, 8192, lambda data: {'data': data})
                self.assertIsNotNone(fitted)
                raw = decoded(value)
                self.assertEqual(raw, self.source[offset:value['next_offset']])
                self.assertGreater(len(raw), 0)
                self.assertEqual(value['next_read']['arguments']['offset'], offset + len(raw))
                self.assertNotIn('base64', value)

    def test_content_search_cursor_does_not_skip_multibyte_matches(self):
        arguments = {'resource': 'vendor', 'query': '当前', 'limit': 50}
        positions = []
        for _ in range(30):
            report = self.json_result(self.invoke('sdk_search', arguments))
            positions.extend(match['offset'] for match in report['matches'])
            if report['complete']:
                break
            arguments['cursor'] = report['next_cursor']
        expected, start = [], 0
        while True:
            found = self.source.find('当前'.encode(), start)
            if found < 0:
                break
            expected.append(found)
            start = found + len('当前'.encode())
        self.assertEqual(positions, expected)
        self.assertEqual(len(positions), 1200)
        with self.assertRaisesRegex(driver.Rejected, 'different_query_cursor'):
            driver.search(self.config, {**arguments, 'query': 'different'}, self.base / 'unused')

    def test_5000_path_entries_locate_one_late_header_in_one_call(self):
        source = self.base / 'many-sdk'
        (source / 'include').mkdir(parents=True)
        for i in range(5000):
            (source / 'include' / ('header-%04d.h' % i)).write_text('int declaration_%d();\n' % i)
        snapshot.export_snapshot(source, self.base / 'many-snapshot', name='many', version='1', target='fixture', include=['include'])
        prepared = prepare({'resources': [{'id': 'many', 'manifest': str(self.base / 'many-snapshot/manifest.json')}]}, self.base / 'many-tools', sys.executable)
        config = json.loads((self.base / 'many-tools/config.json').read_text())
        with patch.object(driver, 'file_bytes', wraps=driver.file_bytes) as read:
            result = driver.main(config, {'id': 'late-path', 'tool': 'sdk_search',
                'arguments': {'resource': 'many', 'mode': 'path', 'query': 'header-4999.h'}})
        report = json.loads(decoded(result['data']))
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(report['complete'])
        self.assertEqual(report['path_entries_scanned'], 5000)
        self.assertEqual(report['contents_verified'], 1)
        self.assertEqual(report['content_scanned_files'], 0)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(report['matches'][0]['path'], 'include/header-4999.h')
        self.assertEqual(len(prepared['business_tools']), 2)

    def test_real_gateway_replay_preserves_original_and_new_calls_reject_changed_corpus(self):
        gateway, store, task = self.gateway()
        action = Action(id='original', task=task.id, tool='sdk_read', arguments={'resource': 'vendor', 'path': 'include/api.h'})
        first = gateway.execute(action)
        self.assertEqual(first.status, 'completed', first)
        before = len(store.snapshot()['actions'])
        self.changed_file()
        self.assertEqual(gateway.execute(action), first)
        self.assertEqual(len(store.snapshot()['actions']), before)
        changed = gateway.execute(Action(id='new-call', task=task.id, tool='sdk_read', arguments=action.arguments))
        self.assertEqual(changed.status, 'failed', changed)
        self.assertIn('snapshot_file_changed', changed.data['reason'])
        with self.assertRaises(Conflict):
            gateway.execute(Action(id='original', task=task.id, tool='sdk_read', arguments={'resource': 'vendor'}))

    def test_real_context_projection_preserves_all_source_bytes_across_8192_envelopes(self):
        self.assert_projected_source_bytes()

    def assert_projected_source_bytes(self, *, require_base64=False):
        gateway, store, task = self.gateway()
        arguments = {'resource': 'vendor', 'path': 'include/api.h', 'limit': 16384}
        accumulated, shrunk, base64_seen = b'', False, False
        for index in range(100):
            outcome = gateway.execute(Action(id='page-' + str(index), task=task.id, tool='sdk_read', arguments=arguments))
            self.assertEqual(outcome.status, 'completed', outcome)
            projected = project(task, store.policy(), store.snapshot(), [])
            observation = projected['history'][-1]
            value = observation['outcome']['data']
            self.assertNotIn('json_preview', value)
            self.assertEqual(value['sha256'], hashlib.sha256(self.source).hexdigest())
            self.assertEqual(value['offset'], len(accumulated))
            self.assertLessEqual(len(canonical({k: observation[k] for k in ('outcome', 'receipt')}).encode()), 8192)
            shrunk |= value['next_offset'] < outcome.data['next_offset']
            base64_seen |= value['encoding'] == 'base64'
            accumulated += decoded(value)
            if value['eof']:
                break
            arguments = value['next_read']['arguments']
        self.assertTrue(shrunk, 'fixture must exercise real context trimming')
        self.assertEqual(accumulated, self.source)
        if require_base64:
            self.assertTrue(base64_seen, 'fixture must exercise actual base64 byte projection')

    def test_non_utf8_source_survives_real_gateway_and_8192_context_projection(self):
        raw = b'// Original non-UTF8 comment: \xff\xfe\x80\nint NativeApi();\n' * 1500
        self.install_raw_fixture(raw)
        retained = self.base / 'raw-snapshot/files/include/api.h'
        self.assertEqual(retained.read_bytes(), raw)
        self.assertEqual(self.manifest['files'][0]['sha256'], hashlib.sha256(raw).hexdigest())
        read = self.invoke(arguments={'resource': 'vendor', 'path': 'include/api.h'})['data']
        self.assertFalse(read['source_utf8_valid'])
        self.assertEqual(read['encoding'], 'base64')
        self.assert_projected_source_bytes(require_base64=True)

    def test_non_utf8_snippet_is_lossless_and_query_scope_is_byte_explicit(self):
        # These bytes are deliberately not UTF8. Their source codec is not
        # supplied; the driver must neither guess one nor silently replace it.
        raw = b'// \xd6\xd0\xce\xc4 NativeApi declaration\nint NativeApi();\n'
        self.install_raw_fixture(raw)
        report = self.json_result(self.invoke('sdk_search', {'resource': 'vendor', 'query': 'NativeApi'}))
        self.assertTrue(report['complete'])
        self.assertEqual(report['query_encoding'], 'utf-8')
        self.assertEqual(report['match_semantics'], 'literal_utf8_byte_sequence')
        self.assertEqual(report['non_utf8_files_verified'], 1)
        first = report['matches'][0]
        self.assertEqual(first['encoding'], 'base64')
        original = raw[first['text_offset']:first['text_offset'] + first['text_size_bytes']]
        self.assertEqual(decoded(first), original)
        self.assertEqual(first['text_sha256'], hashlib.sha256(original).hexdigest())
        self.assertFalse(first['source_utf8_valid'])
        second = report['matches'][1]
        self.assertEqual(second['encoding'], 'utf-8')
        self.assertEqual(decoded(second), b'int NativeApi();')
        absent = self.json_result(self.invoke('sdk_search', {'resource': 'vendor', 'query': '中文'}))
        self.assertTrue(absent['complete'])
        self.assertEqual(absent['matches'], [])
        self.assertEqual(absent['non_utf8_files_verified'], 1)
        self.assertIn('cannot establish absence of equivalent characters', absent['scope'])

    def test_original_captured_non_utf8_sdk_header_read_search_and_projection(self):
        path = os.environ.get('KERNELBRIDGE_SDK_NON_UTF8_FIXTURE')
        expected = os.environ.get('KERNELBRIDGE_SDK_NON_UTF8_SHA256')
        if not path or not expected:
            self.skipTest('Original device SDK header is an external provenance-bound fixture')
        fixture = Path(path)
        raw = fixture.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), expected)
        self.assertNotIn(b'\0', raw)
        with self.assertRaises(UnicodeDecodeError):
            raw.decode('utf-8')
        self.install_raw_fixture(raw)
        query = re.search(rb'\b[A-Za-z_][A-Za-z_0-9]{8,}\b', raw).group().decode('ascii')
        report = self.json_result(self.invoke('sdk_search', {'resource': 'vendor', 'query': query}))
        self.assertGreater(len(report['matches']), 0)
        self.assertEqual(report['non_utf8_files_verified'], 1)
        for match in report['matches']:
            self.assertEqual(match['sha256'], expected)
            begin, size = match['text_offset'], match['text_size_bytes']
            self.assertEqual(decoded(match), raw[begin:begin + size])
        self.assert_projected_source_bytes(require_base64=True)
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), expected)

    def test_search_report_projection_recovers_full_report_without_rescanning(self):
        outcome = self.invoke('sdk_search', {'resource': 'vendor', 'query': 'OriginalSDKDeclaration', 'limit': 50})
        original = outcome['data']
        fitted = fit_observation_page(original, 8192, lambda data: {'status': 'completed', 'data': data, 'receipt': {'action_id': 'test'}})
        self.assertIsNotNone(fitted)
        self.assertLess(fitted['next_offset'], original['next_offset'])
        raw, value = b'', fitted
        with patch.object(driver, 'load_resource', side_effect=AssertionError('do not repeat search')):
            for _ in range(20):
                raw += decoded(value)
                if value['eof']:
                    break
                value = self.invoke(arguments=value['next_read']['arguments'])['data']
        report = json.loads(raw)
        self.assertEqual(len(report['matches']), 50)
        self.assertFalse(report['complete'])
        self.assertIsInstance(report['next_cursor'], str)

    def test_changed_pinned_manifest_rejected_before_business_process(self):
        gateway, _, task = self.gateway()
        path = Path(self.config['resources'][0]['manifest'])
        path.chmod(0o600)
        path.write_text('{}')
        outcome = gateway.execute(Action(id='changed-manifest', task=task.id, tool='sdk_read', arguments={}))
        self.assertEqual(outcome.status, 'failed')
        self.assertEqual(outcome.data, {'reason': 'business_tool_input_changed', 'unexecuted': True})


if __name__ == '__main__':
    unittest.main()
