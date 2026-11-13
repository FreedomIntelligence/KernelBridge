"""CPU-only initialization control tests; no target or framework invocation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
import prepare_npu_isolation as client


class InitializationControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='npu-profile-client-')
        self.root = Path(self.temporary.name).resolve()
        self.calls = self.root / 'transport-calls.jsonl'
        self.reply = self.root / 'reply.json'
        self.reply.write_text(json.dumps({'status': 'unknown', 'artifacts': []}))
        self.transport = self.root / 'transport.py'
        self.transport.write_text('import json\nfrom pathlib import Path\n'
            'def run(argv, stdin=b"", timeout=45):\n'
            ' value=json.loads(stdin)\n'
            f' with Path({str(self.calls)!r}).open("a") as stream: stream.write(json.dumps(value)+"\\n")\n'
            ' if "files" in value: raise TimeoutError("ACK deliberately lost")\n'
            f' return {{"returncode":0,"stdout":Path({str(self.reply)!r}).read_bytes(),"stderr":b""}}\n')
        self.profile = self.root / 'input.json'
        self.profile.write_text(json.dumps({'sdk': '/authorized/sdk', 'python': '/authorized/python',
            'provider_secret': 'test-secret-must-stay-local', 'online_research': {'api_key': 'local-only'}}))
        self.output = self.root / 'initialized'
        self.args = SimpleNamespace(output=self.output, profile=self.profile, transport=self.transport,
            transport_dependency=[], device=1, remote_root='/authorized/work', lease_root=None)

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self):
        result = client.prepare(self.args)
        self.assertEqual(result['status'], 'prepared')
        return json.loads((self.output / 'request.json').read_bytes())

    def call_records(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_prepare_is_local_explicit_device_and_redacts_remote_profile(self):
        request = self.prepare()
        self.assertFalse(self.calls.exists())
        self.assertEqual(request['isolation_policy']['logical_device'], 1)
        self.assertNotIn('view_evidence', request['isolation_policy'])
        self.assertEqual(set(json.loads((self.output / 'profile.json').read_bytes())), {'sdk', 'python'})
        self.assertEqual((self.output / 'input-profile.json').read_bytes(), self.profile.read_bytes())
        for path in self.output.rglob('*'):
            if path.is_file() and path.name != 'input-profile.json':
                self.assertNotIn(b'test-secret-must-stay-local', path.read_bytes())
        self.assertEqual((self.output / 'input-profile.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.output / 'initialized-profile.json').exists())

    def test_lost_ack_never_dispatches_twice_and_recovery_is_read_only(self):
        request = self.prepare()
        first = client.dispatch(self.output)
        self.assertEqual(first['status'], 'unknown')
        self.assertTrue((self.output / 'dispatch-intent.json').exists())
        with self.assertRaises(FileExistsError): client.dispatch(self.output)
        self.assertEqual(len(self.call_records()), 1)
        self.assertEqual(client.recover(self.output)['status'], 'unknown')
        calls = self.call_records()
        self.assertEqual(len(calls), 2)
        self.assertIn('files', calls[0]); self.assertNotIn('files', calls[1])
        self.assertEqual(calls[1]['request'], request)
        self.assertFalse((self.output / 'initialized-profile.json').exists())

    def test_original_source_pin_change_stops_before_transport(self):
        self.prepare()
        (self.output / 'tools/npu_enter.py').write_text('# replaced source\n')
        with self.assertRaisesRegex(ValueError, 'payload changed'): client.dispatch(self.output)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.output / 'dispatch-intent.json').exists())

    def test_linked_original_payload_is_rejected(self):
        self.prepare()
        original = self.output / 'tools/npu_enter.py'
        copy = self.root / 'same-entry.py'; copy.write_bytes(original.read_bytes())
        original.unlink(); original.symlink_to(copy)
        with self.assertRaisesRegex(ValueError, 'linked'): client.dispatch(self.output)
        self.assertFalse(self.calls.exists())

    def test_transport_dependency_change_never_sends_payload(self):
        dependency = self.root / 'transport-helper.py'; dependency.write_text('value=1\n')
        self.args.transport_dependency = [dependency]
        self.prepare(); dependency.write_text('value=2\n')
        with self.assertRaisesRegex(ValueError, 'transport source changed'): client.dispatch(self.output)
        self.assertFalse(self.calls.exists())
        self.assertTrue((self.output / 'dispatch-intent.json').exists())

    def test_retained_client_cli_recovers_original_request(self):
        self.prepare(); client.dispatch(self.output)
        reply = subprocess.run([sys.executable, '-B', str(self.output / 'tools/prepare_npu_isolation.py'),
            'recover', '--output', str(self.output)], capture_output=True, text=True, timeout=10)
        self.assertEqual(reply.returncode, 3, reply.stderr)
        self.assertEqual(json.loads(reply.stdout)['status'], 'unknown')
        self.assertEqual(len(self.call_records()), 2)
        self.assertNotIn('files', self.call_records()[1])

    def test_initialize_cli_prepares_once_and_keeps_uncertain_original_for_recovery(self):
        argv = [sys.executable, '-B', str(Path(client.__file__)), 'initialize', '--profile', str(self.profile),
                '--transport', str(self.transport), '--remote-root', self.args.remote_root, '--device', '1',
                '--output', str(self.output), '--wait-seconds', '0']
        reply = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertEqual(reply.returncode, 3, reply.stderr)
        self.assertEqual(json.loads(reply.stdout)['status'], 'unknown')
        self.assertEqual(len(self.call_records()), 2)
        repeat = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(repeat.returncode, 0)
        self.assertEqual(len(self.call_records()), 2)
        self.assertTrue((self.output / 'dispatch-intent.json').is_file())
        self.assertFalse((self.output / 'initialized-profile.json').exists())

    def test_real_cpu_stage_is_single_dispatch_and_collect_respects_bound_root_alias(self):
        authorized = self.root / 'authorized'; authorized.mkdir()
        alias = self.root / 'authorized-alias'; alias.symlink_to(authorized, target_is_directory=True)
        sentinel = authorized / '.npu-job.lock'; sentinel.write_bytes(b'original deployment lock untouched')
        sdk = self.root / 'sdk'; sdk.mkdir()
        (sdk / 'set_env.sh').write_text('export ASCEND_HOME_PATH=' + shlex.quote(str(sdk)) + '\n')
        self.profile.write_text(json.dumps({'sdk': str(sdk), 'python': sys.executable}))
        self.args.remote_root = str(alias)
        request = self.prepare(); raw = (self.output / 'request.json').read_bytes()
        body = {'request': base64.b64encode(raw).decode(), 'request_sha256': client.digest(raw),
                'files': {name: base64.b64encode((self.output / name).read_bytes()).decode()
                          for name in (*request['required_files'], 'npu_pid_view.py')}}
        # The real receiver rejects this inherited remap before npu-smi or
        # importing torch. Only entry/controller Python subprocesses execute.
        env = dict(os.environ, ASCEND_VISIBLE_DEVICES='unsupported-test-remap')
        def run(source, value):
            return subprocess.run([sys.executable, '-B', '-c', source], input=client.canonical(value),
                capture_output=True, env=env, timeout=10)
        first = run(client.STAGE, body)
        self.assertEqual(first.returncode, 0, first.stderr)
        folder = authorized / '.infrastructure-probes' / request['probe_id']
        ack = (folder / 'dispatch-ack.json').read_bytes()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b'Visibility remapping' not in (folder / 'controller.log').read_bytes():
            time.sleep(.02)
        self.assertIn(b'Visibility remapping', (folder / 'controller.log').read_bytes())
        self.assertFalse((folder / 'holder-start.json').exists())
        second = run(client.STAGE, body)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual((folder / 'dispatch-ack.json').read_bytes(), ack)
        collected = run(client.COLLECT, {'request': request, 'request_sha256': client.digest(raw)})
        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertEqual(json.loads(collected.stdout)['status'], 'observed')
        entry = folder / 'tools/npu_enter.py'; external = self.root / 'same-entry.py'
        external.write_bytes(entry.read_bytes()); entry.unlink(); entry.symlink_to(external)
        linked = run(client.COLLECT, {'request': request, 'request_sha256': client.digest(raw)})
        self.assertNotEqual(linked.returncode, 0)
        self.assertIn(b'linked', linked.stderr)
        self.assertEqual(sentinel.read_bytes(), b'original deployment lock untouched')

    def packet(self, *, device=1):
        from npu_pid_view_fixture import build_packet
        packet = build_packet(self.output, logical_device=device)
        request = json.loads(packet['request_raw'])
        client.save_bytes(self.output / 'input-profile.json', self.profile.read_bytes())
        client.save(self.output / 'prepared.json', {'request_sha256': client.digest(packet['request_raw']),
            'transport_module': str(self.transport), 'transport_code_pins': {str(self.transport): client.digest(self.transport.read_bytes())},
            'input_profile_sha256': client.digest(self.profile.read_bytes()),
            'client_sha256': client.digest(Path(client.__file__).read_bytes())})
        client.save(self.output / 'dispatch-intent.json', {'request_sha256': client.digest(packet['request_raw'])})
        if not (self.output / 'request.json').exists(): client.save_bytes(self.output / 'request.json', packet['request_raw'])
        self.packet_reply(packet['artifacts'])
        return packet, request

    def packet_reply(self, artifacts):
        self.reply.write_text(json.dumps({'status': 'observed', 'artifacts': [
            {'name': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
             'base64': base64.b64encode(raw).decode()} for name, raw in artifacts.items()]}))

    def test_original_raw_packet_qualifies_profile_via_real_validator(self):
        packet, request = self.packet(device=1)
        result = client.recover(self.output)
        self.assertEqual(result['status'], 'completed', result)
        profile = json.loads(Path(result['profile']).read_bytes())
        self.assertEqual(profile['device'], 1)
        self.assertEqual(profile['device_isolation']['policy'],
                         {**packet['policy'], 'view_evidence': packet['records']['view-evidence-candidate.json']})
        self.assertEqual(profile['provider_secret'], 'test-secret-must-stay-local')
        before = Path(result['profile']).read_bytes()
        self.assertEqual(client.recover(self.output)['status'], 'completed')
        self.assertEqual(Path(result['profile']).read_bytes(), before)
        self.assertTrue(all('files' not in call for call in self.call_records()))
        self.assertEqual(self.call_records()[0]['request'], request)

    def test_terminal_word_without_original_normal_exit_never_creates_profile(self):
        packet, _ = self.packet()
        changed = dict(packet['artifacts']); changed.pop('holder-exit.json')
        self.packet_reply(changed)
        result = client.recover(self.output)
        self.assertEqual(result['status'], 'unqualified', result)
        self.assertFalse((self.output / 'initialized-profile.json').exists())
        self.assertTrue(all('files' not in call for call in self.call_records()))

    def test_page_hash_mismatch_cannot_reach_profile_validator(self):
        self.packet()
        reply = json.loads(self.reply.read_bytes()); reply['artifacts'][0]['sha256'] = '0' * 64
        self.reply.write_text(json.dumps(reply))
        with self.assertRaisesRegex(ValueError, 'artifact identity'): client.recover(self.output)
        self.assertFalse((self.output / 'initialized-profile.json').exists())


if __name__ == '__main__': unittest.main()
