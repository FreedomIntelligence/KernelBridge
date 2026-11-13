"""CPU-only qualification of complete, source-bound PID-view probe packets."""
import ast
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from npu_pid_view_fixture import build_packet, canonical, digest, seal, snapshot


class PidViewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(); self.number = 0

    def packet(self, **kwargs):
        self.number += 1
        return build_packet(self.root / str(self.number), **kwargs)

    def validate(self, f):
        return f['receiver'].validate_probe_evidence(f['request_raw'], f['artifacts'])

    def test_real_validator_supports_device_one_and_explicit_multi_chip_domain(self):
        for device, domain in ((0, None), (1, None), (1, [0, 1])):
            with self.subTest(device=device, domain=domain):
                f = self.packet(logical_device=device, domain_physical_ids=domain)
                policy = self.validate(f)
                self.assertEqual(policy['logical_device'], device)
                self.assertEqual(policy.get('domain_physical_ids'), domain)
                self.assertEqual(policy['view_evidence']['device_key'], f['receiver'].target_key(f['samples']['before']))
                self.assertNotIn('performance', policy)

    def test_validator_does_not_observe_proc_run_commands_or_initialize_torch(self):
        f = self.packet(); original_bytes, original_text = Path.read_bytes, Path.read_text
        def read_bytes(path, *args, **kwargs):
            if str(path).startswith('/proc/'): raise AssertionError('No proc observation in pure validation')
            return original_bytes(path, *args, **kwargs)
        def read_text(path, *args, **kwargs):
            if str(path).startswith('/proc/'): raise AssertionError('No proc observation in pure validation')
            return original_text(path, *args, **kwargs)
        with patch.object(Path, 'read_bytes', read_bytes), patch.object(Path, 'read_text', read_text), \
             patch('subprocess.run', side_effect=AssertionError('No process calls')), \
             patch('subprocess.Popen', side_effect=AssertionError('No child launch')), \
             patch.object(f['receiver'], 'holder', side_effect=AssertionError('No holder')):
            self.assertEqual(self.validate(f)['logical_device'], 0)

    def test_late_or_partial_provisional_ack_is_retained_without_becoming_authority(self):
        f = self.packet(); original_index = f['artifacts']['probe-result.json']
        f['artifacts']['dispatch-ack.json'] = b'{"late-provisional":'
        self.assertEqual(self.validate(f)['logical_device'], 0)
        self.assertEqual(f['artifacts']['probe-result.json'], original_index)
        self.assertNotIn('dispatch-ack.json', [item['name'] for item in f['records']['probe-result.json']['original_artifacts']])

    def test_extra_unindexed_authoritative_json_is_rejected(self):
        f = self.packet(); f['artifacts']['replacement-exit.json'] = b'{}\n'
        with self.assertRaisesRegex(ValueError, 'index'): self.validate(f)

    def test_missing_exit_cannot_be_repaired_by_terminal_success_text(self):
        f = self.packet(); del f['records']['holder-exit.json']; del f['artifacts']['holder-exit.json']; seal(f)
        self.assertEqual(f['records']['probe-result.json']['outcome'], 'candidate_view_evidence_requires_root_admission')
        with self.assertRaisesRegex(ValueError, 'incomplete'): self.validate(f)

    def test_nonzero_or_signal_exit_is_not_a_ready_profile(self):
        for change in ({'returncode': 1}, {'returncode': -15, 'signal': 15}, {'signal': 9}):
            with self.subTest(change=change):
                f = self.packet(); f['records']['holder-exit.json'].update(change); seal(f)
                with self.assertRaisesRegex(ValueError, 'normal OS exit'): self.validate(f)

    def test_original_source_and_profile_pins_survive_reindexed_tampering(self):
        for name in ('profile.json', 'tools/npu_enter.py', 'tools/npu_isolation.py', 'tools/process_isolation.py', 'npu_pid_view.py'):
            with self.subTest(name=name):
                f = self.packet(); f['artifacts'][name] += b'\n'; seal(f)
                with self.assertRaisesRegex(ValueError, 'payload hash|receiver bytes'): self.validate(f)

    def test_local_snapshot_helper_replacement_is_not_loaded(self):
        f = self.packet(); path = f['folder'] / 'tools/npu_isolation.py'; path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'Pinned isolation payload'): self.validate(f)

    def test_inner_helper_symlink_is_not_an_admitted_code_origin(self):
        f = self.packet(); path = f['folder'] / 'tools/npu_isolation.py'; other = self.root / 'other.py'
        other.write_bytes(path.read_bytes()); path.unlink(); path.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'Pinned isolation payload'): self.validate(f)

    def test_rebinding_request_device_does_not_relabel_old_physical_proof(self):
        f = self.packet(domain_physical_ids=[0, 1]); f['request']['isolation_policy']['logical_device'] = 1
        f['request_raw'] = canonical(f['request']) + b'\n'; request_sha = digest(f['request_raw'])
        for record in f['records'].values():
            if 'request_sha256' in record: record['request_sha256'] = request_sha
        seal(f)
        with self.assertRaisesRegex(ValueError, 'minimal-device contract|retained raw tables'): self.validate(f)

    def test_holder_birth_namespace_parent_and_visibility_cannot_be_relabelled(self):
        mutations = (
            lambda r: r['identity']['birth'].update(start_ticks='201'),
            lambda r: r['identity'].update(pid_ns='pid:[999]'),
            lambda r: r['identity'].update(ppid=101),
            lambda r: r['visibility'].update(ASCEND_VISIBLE_DEVICES='1'),
        )
        for mutate in mutations:
            f = self.packet(); mutate(f['records']['holder-ready.json']); seal(f)
            with self.assertRaises(ValueError): self.validate(f)

    def test_scope_changes_are_rejected_even_with_new_artifact_hashes(self):
        for section, key, value in (('driver', 'sha256', 'e' * 64), ('observer', 'mnt_ns', 'mnt:[999]')):
            f = self.packet(); f['records']['after.json'][section][key] = value; seal(f)
            with self.assertRaisesRegex(ValueError, 'scope'): self.validate(f)

    def test_foreign_occupancy_after_normal_exit_does_not_qualify_view(self):
        f = self.packet(); f['records']['after.json'].update(snapshot([(0, 700)])); seal(f)
        with self.assertRaisesRegex(ValueError, 'occupancy'): self.validate(f)

    def test_empty_truncated_process_table_does_not_prove_idle(self):
        f = self.packet(); sample = f['records']['before.json']; lines = sample['inventory']['stdout'].splitlines()
        i = next(i for i, line in enumerate(lines) if 'Process id' in line)
        sample['inventory']['stdout'] = '\n'.join(lines[:i + 1]); seal(f)
        with self.assertRaises(ValueError): self.validate(f)

    def test_view_projection_cannot_replace_raw_pid_observations(self):
        f = self.packet(); f['records']['view-evidence-candidate.json']['probe'].update(reported_pid=999, active_pids=[999]); seal(f)
        with self.assertRaisesRegex(ValueError, 'projection'): self.validate(f)

    def test_expanded_domain_lease_and_actual_target_are_both_required(self):
        f = self.packet(logical_device=1, domain_physical_ids=[0, 1])
        f['records']['lease-admission.json']['device_keys'].pop(); seal(f)
        with self.assertRaisesRegex(ValueError, 'lease/domain'): self.validate(f)
        f = self.packet(logical_device=1, domain_physical_ids=[0, 1])
        f['records']['active.json'].update(snapshot([(0, 200)])); seal(f)
        with self.assertRaisesRegex(ValueError, 'different physical target'): self.validate(f)

    def test_outside_domain_process_is_retained_but_not_claimed_as_target_occupancy(self):
        f = self.packet()
        for phase in ('before-lease', 'before', 'active', 'after'):
            rows = [(1, 800)] + ([(0, 200)] if phase == 'active' else [])
            f['records'][phase + '.json'].update(snapshot(rows))
        view = f['records']['view-evidence-candidate.json']
        view['raw_snapshots'] = {phase: {key: copy.deepcopy(f['records'][phase + '.json'][key]) for key in ('mapping', 'inventory')}
                                 for phase in ('before', 'active', 'after')}
        view['probe']['evidence_sha256'] = digest(canonical(view['raw_snapshots'])); seal(f)
        self.assertEqual(self.validate(f)['logical_device'], 0)
        self.assertIn('800', f['records']['before.json']['inventory']['stdout'])

    def test_ready_receipt_atomic_publication_and_no_overwrite(self):
        f = self.packet(); path = self.root / 'ready.json'; receiver = f['receiver']; value = {'identity': {'pid': 200}}
        reached = threading.Event(); release = threading.Event(); errors = []; link = receiver.os.link
        def before_publish(source, target, **kwargs):
            self.assertFalse(path.exists()); self.assertEqual(json.loads(Path(source).read_bytes()), value); reached.set()
            if not release.wait(2): raise AssertionError('Reader did not release publication')
            return link(source, target, **kwargs)
        def writer():
            try: receiver.save(path, value)
            except BaseException as error: errors.append(error)
        with patch.object(receiver.os, 'link', side_effect=before_publish):
            thread = threading.Thread(target=writer); thread.start()
            try: self.assertTrue(reached.wait(2)); self.assertFalse(path.exists())
            finally: release.set(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, []); original = path.read_bytes()
        with self.assertRaises(FileExistsError): receiver.save(path, {'replacement': True})
        self.assertEqual(path.read_bytes(), original); self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_holder_contract_has_only_explicit_device_empty_and_normal_stop(self):
        f = self.packet(); tree = ast.parse((f['folder'] / 'npu_pid_view.py').read_text())
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        holder = ast.unparse(functions['holder']); controller = ast.unparse(functions['controller'])
        self.assertIn("torch.device('npu', policy['logical_device'])", holder)
        self.assertIn('torch.empty((1,), dtype=torch.uint8, device=device)', holder)
        self.assertIn("not (folder / 'stop-holder.json').exists()", holder)
        stop = controller.index("save(folder / 'stop-holder.json'")
        wait = controller.index('returncode = child.wait(timeout=30)', stop)
        after = controller.index("sample(folder, 'after'", wait)
        self.assertLess(stop, wait); self.assertLess(wait, after)
        for text in ('/Users/', 'HongKong', '31470', 'npu:0', '0000:9d:00.0'):
            self.assertNotIn(text, (f['folder'] / 'npu_pid_view.py').read_text())


if __name__ == '__main__': unittest.main()
