"""Local SDK snapshot qualification: no model, network, compiler or device calls."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'examples/sdk_reference/snapshot.py'
spec = importlib.util.spec_from_file_location('sdk_snapshot_test_subject', SCRIPT)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class SDKSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kb-sdk-snapshot-')
        self.base = Path(self.temporary.name).resolve()
        self.sdk = self.base / 'installed-sdk'
        (self.sdk / 'include').mkdir(parents=True)
        self.header = self.sdk / 'include/api.h'
        self.header.write_text('// SDK public declaration\nint query_shape();\n', encoding='utf-8')
        self.output = self.base / 'snapshot'
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for folder, directories, files in os.walk(self.base):
            if not Path(folder).is_symlink():
                Path(folder).chmod(0o700)
            for name in files:
                path = Path(folder) / name
                if not path.is_symlink():
                    path.chmod(0o600)
            for name in directories:
                path = Path(folder) / name
                if not path.is_symlink():
                    path.chmod(0o700)
        self.temporary.cleanup()

    def export(self, output=None, **changes):
        arguments = {'name': 'fixture-sdk', 'version': '1.2.3', 'target': 'fixture-target',
                     'include': ['include'], 'origin': {'provision_id': 'local-fixture'}}
        arguments.update(changes)
        return snapshot.export_snapshot(self.sdk, output or self.output, **arguments)

    def assert_failed_without_manifest(self, output=None):
        output = output or self.output
        self.assertFalse((output / 'manifest.json').exists())
        status = json.loads((output / 'export-failure.json').read_text())
        self.assertEqual(status['status'], 'failed')
        self.assertTrue(status['error_type'])

    def test_exported_bytes_and_manifest_are_independently_verifiable(self):
        original = self.header.read_bytes()
        manifest = self.export()
        restored = json.loads((self.output / 'manifest.json').read_bytes())
        self.assertEqual(restored, manifest)
        identity = restored.pop('snapshot_id')
        canonical = json.dumps(restored, sort_keys=True, ensure_ascii=False,
                               separators=(',', ':'), allow_nan=False).encode('utf-8')
        self.assertEqual(identity, hashlib.sha256(canonical).hexdigest())
        self.assertEqual(manifest['schema_version'], 'kernelbridge.sdk-snapshot/v1')
        self.assertEqual(manifest['origin']['canonical_root'], str(self.sdk))
        self.assertEqual(manifest['origin']['provision_id'], 'local-fixture')
        self.assertEqual(manifest['export']['include'], ['include'])
        item = manifest['files'][0]
        copied = self.output / 'files' / item['path']
        self.assertEqual(item['path'], 'include/api.h')
        self.assertEqual(item['source_path'], str(self.header))
        self.assertEqual(item['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(item['bytes'], len(original))
        self.assertEqual(copied.read_bytes(), original)
        self.assertNotEqual(copied.stat().st_ino, self.header.stat().st_ino)
        self.assertEqual(copied.stat().st_mode & 0o222, 0)
        self.assertEqual(self.header.read_bytes(), original)

    def test_internal_file_and_directory_links_become_regular_logical_files(self):
        real = self.sdk / 'platform/public'
        real.mkdir(parents=True)
        declaration = real / 'linked.h'
        declaration.write_text('int device_agnostic_api();\n')
        (self.sdk / 'include/alias.h').symlink_to('../platform/public/linked.h')
        (self.sdk / 'include/subdir').symlink_to('../platform/public', target_is_directory=True)
        manifest = self.export()
        entries = {row['path']: row for row in manifest['files']}
        self.assertEqual(set(entries), {'include/api.h', 'include/alias.h', 'include/subdir/linked.h'})
        for name, link in [('include/alias.h', 'include/alias.h'),
                           ('include/subdir/linked.h', 'include/subdir')]:
            copied = self.output / 'files' / name
            self.assertTrue(stat.S_ISREG(copied.lstat().st_mode))
            self.assertFalse(copied.is_symlink())
            self.assertEqual(copied.read_bytes(), declaration.read_bytes())
            self.assertEqual(entries[name]['source_path'], str(declaration))
            self.assertEqual(entries[name]['source_links'][0]['path'], link)
            self.assertEqual(entries[name]['source_links'][0]['resolved_path'],
                             str(declaration if name.endswith('alias.h') else real))

    def test_internal_link_in_include_parent_is_retained_in_provenance(self):
        real = self.sdk / 'platform/public'
        real.mkdir(parents=True)
        (real / 'nested.h').write_text('void nested_api();\n')
        (self.sdk / 'aliases').symlink_to('platform', target_is_directory=True)
        manifest = self.export(include=['aliases/public'])
        item = manifest['files'][0]
        self.assertEqual(item['path'], 'aliases/public/nested.h')
        self.assertEqual(item['source_links'][0]['path'], 'aliases')
        self.assertEqual(item['source_links'][0]['resolved_path'], str(self.sdk / 'platform'))

    def test_external_file_link_is_rejected_without_disclosing_target_bytes(self):
        secret = self.base / 'outside.h'
        secret.write_text('outside-fixture-sentinel\n')
        (self.sdk / 'include/external.h').symlink_to(secret)
        with self.assertRaisesRegex(snapshot.SnapshotError, 'escapes canonical root'):
            self.export()
        self.assert_failed_without_manifest()
        self.assertNotIn('outside-fixture-sentinel',
                         (self.output / 'export-failure.json').read_text())

    def test_external_directory_and_nested_include_parent_are_rejected(self):
        outside = self.base / 'outside/public'
        outside.mkdir(parents=True)
        (outside / 'external.h').write_text('int private_api();\n')
        (self.sdk / 'include/external').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(snapshot.SnapshotError, 'escapes canonical root'):
            self.export()
        self.assert_failed_without_manifest()
        (self.sdk / 'alias').symlink_to(outside.parent, target_is_directory=True)
        second = self.base / 'nested-escape'
        with self.assertRaisesRegex(snapshot.SnapshotError, 'escapes canonical root'):
            self.export(output=second, include=['alias/public'])
        self.assert_failed_without_manifest(second)

    def test_cycle_and_dangling_link_do_not_publish_partial_snapshot(self):
        link = self.sdk / 'include/loop'
        link.symlink_to('.', target_is_directory=True)
        with self.assertRaisesRegex(snapshot.SnapshotError, 'Cyclic SDK directory'):
            self.export()
        self.assert_failed_without_manifest()
        link.unlink()
        link.symlink_to('missing.h')
        second = self.base / 'dangling'
        with self.assertRaisesRegex(snapshot.SnapshotError, 'Unreadable or cyclic SDK path'):
            self.export(output=second)
        self.assert_failed_without_manifest(second)

    def test_file_changed_after_copy_rejects_export_and_retains_failure(self):
        original_write = snapshot._write_new

        def mutate_after_copy(path, raw):
            original_write(path, raw)
            if path == self.output / 'files/include/api.h':
                self.header.write_text('int changed_during_export();\n')

        with patch.object(snapshot, '_write_new', side_effect=mutate_after_copy):
            with self.assertRaisesRegex(snapshot.SnapshotError, 'changed during export'):
                self.export()
        self.assert_failed_without_manifest()
        self.assertEqual((self.output / 'files/include/api.h').read_text(),
                         '// SDK public declaration\nint query_shape();\n')

    def test_added_source_during_copy_is_not_silently_omitted(self):
        original_write = snapshot._write_new

        def add_after_copy(path, raw):
            original_write(path, raw)
            if path == self.output / 'files/include/api.h':
                (self.sdk / 'include/late.h').write_text('int newly_added();\n')

        with patch.object(snapshot, '_write_new', side_effect=add_after_copy):
            with self.assertRaisesRegex(snapshot.SnapshotError, 'inventory changed'):
                self.export()
        self.assert_failed_without_manifest()

    def test_selected_nul_content_fails_rather_than_being_excluded(self):
        for index, raw in enumerate([b'int a;\x00', b'\xff\xfea\x00']):
            with self.subTest(raw=raw):
                self.header.write_bytes(raw)
                output = self.base / ('invalid-text-' + str(index))
                with self.assertRaisesRegex(snapshot.SnapshotError, 'contains NUL'):
                    self.export(output=output)
                self.assert_failed_without_manifest(output)

    def test_unknown_encoding_bytes_are_preserved_and_bound_to_version(self):
        raw = b'// unknown comment encoding: \xff\xfe\x80\x81\xa0\nint unchanged_api();\n'
        with self.assertRaises(UnicodeDecodeError):
            raw.decode('utf-8')
        self.header.write_bytes(raw)
        first = self.export()
        repeated = self.export(output=self.base / 'same-raw-bytes')
        different_version = self.export(output=self.base / 'version-2', version='2.0.0')
        for manifest in [first, repeated, different_version]:
            item = manifest['files'][0]
            self.assertEqual(item['path'], 'include/api.h')
            self.assertEqual(item['bytes'], len(raw))
            self.assertEqual(item['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(manifest['export']['excluded'], [])
            self.assertEqual(manifest['export']['content_policy'],
                             'nul_free_original_bytes; encoding_not_inferred')
        self.assertEqual((self.output / 'files/include/api.h').read_bytes(), raw)
        self.assertEqual((self.base / 'version-2/files/include/api.h').read_bytes(), raw)
        self.assertEqual(self.header.read_bytes(), raw)
        self.assertEqual(first['snapshot_id'], repeated['snapshot_id'])
        self.assertNotEqual(first['snapshot_id'], different_version['snapshot_id'])
        self.assertEqual((first['version'], different_version['version']), ('1.2.3', '2.0.0'))

    def test_unknown_encoding_through_internal_symlink_is_not_transcoded(self):
        raw = b'// comment fragment \xd6\xd0\xce\xc4 \xff\nvoid header_api();\n'
        real = self.sdk / 'platform/public'
        real.mkdir(parents=True)
        declaration = real / 'non_utf8.h'
        declaration.write_bytes(raw)
        (self.sdk / 'include/alias.h').symlink_to('../platform/public/non_utf8.h')
        (self.sdk / 'include/platform').symlink_to('../platform/public', target_is_directory=True)
        manifest = self.export()
        entries = {item['path']: item for item in manifest['files']}
        for name in ['include/alias.h', 'include/platform/non_utf8.h']:
            copied = self.output / 'files' / name
            self.assertTrue(stat.S_ISREG(copied.lstat().st_mode))
            self.assertFalse(copied.is_symlink())
            self.assertEqual(copied.read_bytes(), raw)
            self.assertEqual(entries[name]['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(entries[name]['bytes'], len(raw))
            self.assertEqual(entries[name]['source_path'], str(declaration))
            self.assertTrue(entries[name]['source_links'])
        self.assertEqual(declaration.read_bytes(), raw)

    def test_file_byte_and_total_bounds_are_failures_not_partial_successes(self):
        original = self.header.read_bytes()
        (self.sdk / 'include/second.h').write_bytes(original)
        configurations = [dict(max_file_bytes=len(original) - 1),
                          dict(max_bytes=len(original)), dict(max_files=1)]
        for index, limits in enumerate(configurations):
            with self.subTest(limits=limits):
                output = self.base / ('limited-' + str(index))
                with self.assertRaisesRegex(snapshot.SnapshotError, 'exceeds'):
                    self.export(output=output, **limits)
                self.assert_failed_without_manifest(output)

    def test_unsupported_extensions_are_explicitly_excluded_and_empty_text_is_valid(self):
        (self.sdk / 'include/template.xlsx').write_bytes(b'\x00binary workbook')
        (self.sdk / 'include/empty.h').write_bytes(b'')
        manifest = self.export()
        self.assertEqual(manifest['export']['excluded'], [
            {'path': 'include/template.xlsx', 'bytes': 16, 'reason': 'extension_not_selected'}])
        item = next(row for row in manifest['files'] if row['path'] == 'include/empty.h')
        self.assertEqual(item['bytes'], 0)
        self.assertEqual(item['sha256'], hashlib.sha256(b'').hexdigest())
        self.assertEqual((self.output / 'files/include/empty.h').read_bytes(), b'')

    def test_snapshot_identity_changes_with_version_target_and_exported_content(self):
        original = self.export()
        same = self.export(output=self.base / 'same')
        version = self.export(output=self.base / 'new-version', version='1.2.4')
        target = self.export(output=self.base / 'new-target', target='different-target')
        self.header.write_text('int changed_api();\n')
        changed = self.export(output=self.base / 'new-content')
        self.assertEqual(original['snapshot_id'], same['snapshot_id'])
        self.assertEqual(len({row['snapshot_id'] for row in [original, version, target, changed]}), 4)

    def test_invalid_or_overlapping_include_paths_are_rejected_before_output(self):
        for index, includes in enumerate([['../outside'], ['/absolute'], ['include/../include'],
                                          ['include//nested'], ['include\\nested'],
                                          ['include', 'include'], ['include', 'include/nested']]):
            with self.subTest(includes=includes):
                output = self.base / ('invalid-path-' + str(index))
                with self.assertRaises(snapshot.SnapshotError):
                    self.export(output=output, include=includes)
                self.assertFalse(output.exists())

    def test_output_inside_sdk_and_existing_output_are_not_modified(self):
        with self.assertRaisesRegex(snapshot.SnapshotError, 'outside the source SDK'):
            self.export(output=self.sdk / 'copy')
        self.output.mkdir()
        sentinel = self.output / 'sentinel'
        sentinel.write_bytes(b'preserve-existing-output')
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(sentinel.read_bytes(), b'preserve-existing-output')
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), ['sentinel'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires local POSIX FIFO support')
    def test_nonregular_source_is_rejected_without_opening_it(self):
        os.mkfifo(self.sdk / 'include/pipe.h')
        with self.assertRaisesRegex(snapshot.SnapshotError, 'nonregular entry'):
            self.export()
        self.assert_failed_without_manifest()

    def test_cli_exports_without_importing_or_executing_sdk_python(self):
        sentinel = self.base / 'should-not-exist'
        script = self.sdk / 'include/setup.py'
        script.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ").touch()\n")
        command = [sys.executable, '-I', '-B', str(SCRIPT), '--root', str(self.sdk),
                   '--output', str(self.output), '--name', 'fixture-sdk', '--version', '1.2.3',
                   '--target', 'fixture-target', '--include', 'include']
        result = subprocess.run(command, text=True, capture_output=True, timeout=20,
                                env={'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['files'], 2)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(receipt['snapshot_id'], manifest['snapshot_id'])
        self.assertEqual((self.output / 'files/include/setup.py').read_bytes(), script.read_bytes())


if __name__ == '__main__':
    unittest.main()
