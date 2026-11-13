"""Real Linux CPU-only children for waitid(WNOWAIT) and raw exit identity.

No accelerator, compiler, network, fixture proc tree, or mocked wait call is
used. Copy this file plus examples/operator_pipeline/process_isolation.py with
their relative paths intact to run the same checks on a Linux host.
"""
from contextlib import contextmanager
import copy
import importlib.util
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'examples/operator_pipeline/process_isolation.py'
SPEC = importlib.util.spec_from_file_location('child_exit_fence_processes', SOURCE)
processes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(processes)
LINUX = sys.platform.startswith('linux') and hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT')

CHILD = '''import select, sys
print("ready", flush=True)
ready, _, _ = select.select([sys.stdin], [], [], 5)
if not ready:
    raise SystemExit(98)
sys.stdin.buffer.read(1)
raise SystemExit(int(sys.argv[1]))
'''


@unittest.skipUnless(LINUX, 'requires actual Linux /proc and waitid(WNOWAIT)')
class ChildExitFenceTests(unittest.TestCase):
    @contextmanager
    def child(self, code=0):
        child = subprocess.Popen([sys.executable, '-B', '-c', CHILD, str(code)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ready, _, _ = select.select([child.stdout], [], [], 5)
            self.assertTrue(ready, 'CPU fixture child did not become ready')
            self.assertEqual(child.stdout.readline(), b'ready\n')
            identity = processes.identify_process(child.pid)
            self.assertIsNotNone(identity)
            self.assertEqual(identity['ppid'], os.getpid())
            self.assertNotIn(identity['state'], ('Z', 'X'))
            yield child, identity
        finally:
            # Operate only on this Popen child. No process-tree search or global
            # signal changes; Popen checks the unreaped original child first.
            try:
                child.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None: stream.close()

    def release(self, child):
        child.stdin.write(b'x')
        child.stdin.flush()

    def await_unreaped_exit(self, child):
        """Bound readiness without poll()/wait(), which would consume the PID."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = processes.wait_child_exit(child.pid, nonblocking=True)
            if record is not None: return record
            time.sleep(.01)
        self.fail('Original CPU child did not exit within the bounded wait')

    def test_normal_exit_codes_preserve_pid_birth_until_real_wait(self):
        for expected in (0, 7, 255):
            with self.subTest(returncode=expected), self.child(expected) as (child, identity):
                self.release(child)
                initial = self.await_unreaped_exit(child)
                # Blocking mode now has a known waitable terminal child, so it
                # cannot wait for a live child or an unrelated process.
                again = processes.wait_child_exit(child.pid)
                self.assertEqual(again['options'], ['WEXITED', 'WNOWAIT'])
                self.assertEqual(initial['result'], again['result'])
                self.assertEqual(processes.validate_child_wait(again, pid=child.pid, parent_pid=os.getpid()), expected)
                raw_stat = (Path('/proc') / str(child.pid) / 'stat').read_text()
                fields = raw_stat[raw_stat.rfind(')') + 2:].split()
                self.assertEqual(fields[0], 'Z')
                self.assertEqual(fields[19], identity['birth']['start_ticks'])
                self.assertIsNone(child.returncode, 'WNOWAIT must not reap through Popen')
                self.assertEqual(child.wait(timeout=2), expected)
                with self.assertRaises(ChildProcessError):
                    processes.wait_child_exit(child.pid, nonblocking=True)

    def test_signal_exit_records_actual_signal_without_reaping(self):
        with self.child() as (child, identity):
            child.send_signal(signal.SIGTERM)
            waited = self.await_unreaped_exit(child)
            self.assertEqual(waited['result']['si_code'], 2)  # CLD_KILLED
            self.assertEqual(waited['result']['si_status'], signal.SIGTERM)
            proof = processes.observe_unreaped_child(identity)
            self.assertEqual(processes.validate_unreaped_child(proof, identity, parent_pid=os.getpid()), -signal.SIGTERM)
            self.assertEqual(child.wait(timeout=2), -signal.SIGTERM)

    def test_terminal_observation_retains_real_proc_and_wait_bytes(self):
        with self.child(3) as (child, identity):
            self.release(child)
            self.await_unreaped_exit(child)
            proof = processes.observe_unreaped_child(identity)
            self.assertTrue(proof['stat_before'].startswith(str(child.pid) + ' ('))
            self.assertTrue(proof['stat_after'].startswith(str(child.pid) + ' ('))
            self.assertIn('NSpid:', proof['status_after'])
            self.assertEqual(proof['boot_id'], identity['birth']['boot_id'])
            self.assertEqual(set(proof['namespaces_after']), {'pid', 'mnt'})
            self.assertEqual(processes.validate_unreaped_child(proof, identity, parent_pid=os.getpid()), 3)
            self.assertEqual(processes.wait_child_exit(child.pid, nonblocking=True)['result'], proof['wait']['result'])
            self.assertEqual(child.wait(timeout=2), 3)

    def test_live_child_is_not_a_terminal_observation(self):
        with self.child() as (child, identity):
            self.assertIsNone(processes.wait_child_exit(child.pid, nonblocking=True))
            with self.assertRaisesRegex(processes.IsolationError, 'no waitable OS exit'):
                processes.observe_unreaped_child(identity)
            self.release(child)
            self.await_unreaped_exit(child)
            self.assertEqual(child.wait(timeout=2), 0)

    def test_already_reaped_child_is_echild_not_synthetic_success(self):
        with self.child(9) as (child, identity):
            self.release(child)
            self.assertEqual(child.wait(timeout=5), 9)
            with self.assertRaises(ChildProcessError):
                processes.wait_child_exit(child.pid, nonblocking=True)
            with self.assertRaises((OSError, processes.IsolationError)):
                processes.observe_unreaped_child(identity)

    def test_nonchild_and_missing_identity_are_rejected(self):
        own = processes.identify_process(os.getpid())
        with self.assertRaisesRegex(processes.IsolationError, 'original direct child'):
            processes.observe_unreaped_child(own)
        with self.assertRaises(ChildProcessError):
            processes.wait_child_exit(os.getpid(), nonblocking=True)
        for identity in (None, {}, {'pid': os.getpid()}):
            with self.subTest(identity=identity), self.assertRaises(processes.IsolationError):
                processes.observe_unreaped_child(identity)

    def test_pure_validation_rejects_changed_birth_parent_and_namespace(self):
        with self.child(6) as (child, identity):
            self.release(child)
            self.await_unreaped_exit(child)
            proof = processes.observe_unreaped_child(identity)
            mutations = (
                lambda item: item['birth'].update(start_ticks=str(int(item['birth']['start_ticks']) + 1)),
                lambda item: item.update(ppid=item['ppid'] + 1),
                lambda item: item.pop('nspid'),
            )
            for mutation in mutations:
                altered = copy.deepcopy(identity); mutation(altered)
                with self.subTest(identity=altered), self.assertRaises(processes.IsolationError):
                    processes.validate_unreaped_child(proof, altered, parent_pid=os.getpid())
            for namespace in ('pid', 'mnt'):
                altered = copy.deepcopy(proof)
                altered['namespaces_after'][namespace] = namespace + ':[0]'
                with self.subTest(namespace=namespace), self.assertRaises(processes.IsolationError):
                    processes.validate_unreaped_child(altered, identity, parent_pid=os.getpid())
            self.assertEqual(child.wait(timeout=2), 6)

    def test_pure_validation_rejects_wait_uid_and_nonterminal_result(self):
        with self.child(4) as (child, identity):
            self.release(child)
            self.await_unreaped_exit(child)
            proof = processes.observe_unreaped_child(identity)
            for field, value in (('si_uid', identity['uid'] + 1), ('si_pid', child.pid + 1), ('si_code', 5)):
                altered = copy.deepcopy(proof); altered['wait']['result'][field] = value
                with self.subTest(field=field), self.assertRaises(processes.IsolationError):
                    processes.validate_unreaped_child(altered, identity, parent_pid=os.getpid())
            altered = copy.deepcopy(proof)
            altered['wait']['options'] = ['WEXITED']
            with self.assertRaises(processes.IsolationError):
                processes.validate_unreaped_child(altered, identity, parent_pid=os.getpid())
            self.assertEqual(child.wait(timeout=2), 4)

    def test_sigchld_ignore_rejection_is_isolated_from_test_runner(self):
        # SIGCHLD disposition is changed only in this short-lived CPU helper.
        # Its child has a two-second self-exit bound even on helper failure.
        script = '''import importlib.util, signal, subprocess, sys
spec = importlib.util.spec_from_file_location('isolated_wait', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(2)'])
try:
    try: module.wait_child_exit(child.pid, nonblocking=True)
    except module.IsolationError as error:
        if 'SIGCHLD' not in str(error): raise
        print('SIGCHLD_IGN rejected', flush=True)
    else: raise AssertionError('ignored SIGCHLD was accepted')
finally:
    child.terminate()
    child.wait(timeout=3)
'''
        original = signal.getsignal(signal.SIGCHLD)
        result = subprocess.run([sys.executable, '-B', '-c', script, str(SOURCE)],
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'SIGCHLD_IGN rejected')
        self.assertEqual(signal.getsignal(signal.SIGCHLD), original)


if __name__ == '__main__':
    unittest.main()
