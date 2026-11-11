"""Original-receipt recovery, child cancellation and worktree retention regressions."""
from pathlib import Path
from types import SimpleNamespace
from threading import Event, Thread
from unittest.mock import patch
import os
import shlex
import sys
import time
import unittest

import test_runtime as runtime_support
from kernelcat_executor.capabilities.commands import Worktree, git, run_command_tool
from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, Outcome, Task
from kernelcat_executor.contracts.messages import AssistantTurn, TextContent
from kernelcat_executor.operations.authority import ExecutionRPC
from kernelcat_executor.state.application import ApplicationJournal
from kernelcat_worker.application_host import ApplicationHost
from kernelcat_worker.application_recovery import recovery_plan, reconcile_backgrounds


class RecoveryTests(unittest.TestCase):
    setUp = runtime_support.RuntimeTests.setUp
    tearDown = runtime_support.RuntimeTests.tearDown
    session = runtime_support.RuntimeTests.session

    def authority(self, *, agents=(), limit=6):
        effect = SubagentEffect({'max_concurrent': limit, 'agents': agents})
        owner, _, _ = self.session([], extra_tools=[effect])
        owner._task = self.task
        owner.concurrent_delegation = True
        owner.stopped_children = set()
        self.store.register_task(self.task.model_dump(mode='json'))
        rpc = ExecutionRPC(owner, self.task)
        rpc.active = True
        return owner, rpc

    def begin(self, rpc, identity='one', agent='kernel-scout', background=True):
        action = Action(id=self.task.id + '/delegate/' + identity, task=self.task.id, tool='delegate',
            arguments={'agent': agent, 'task': 'Inspect original receipts', 'background': background})
        rpc.calls = (*rpc.calls, action)
        return action, rpc.plugin_begin(rpc.wire(action))

    def host(self, owner):
        host = ApplicationHost.__new__(ApplicationHost)
        host.owner, host.task = owner, self.task
        host.journal = ApplicationJournal(self.store, 'app-test')
        host.journal.command('create', {'task': self.task.model_dump(mode='json'), 'output': str(self.base)})
        host.journal.command('acquire', {'worker_id': 'worker', 'mode': 'headless', 'recovering': True})
        self.store.bind_application_lease('app-test', 'worker', 1)
        host.broadcasts = []
        host.broadcast = host.broadcasts.append
        return host

    def test_tester_background_rejected_without_child_execution(self):
        owner, rpc = self.authority(agents=[{'name': 'tester', 'description': 'Test', 'tools': 'test'}])
        action, result = self.begin(rpc, agent='tester')
        self.assertTrue(result['cached'])
        self.assertEqual(result['outcome']['data']['reason'], 'background_requires_isolation')
        self.assertIn('worktree worker', result['outcome']['data']['error'])
        self.assertNotIn(action.id + '/leaf', self.store.snapshot()['tasks'])
        self.assertFalse(rpc.children or rpc.background)
        self.assertTrue(self.store.snapshot()['actions'][action.id]['terminal'])

    def test_foreground_tester_blocks_parent_writes_and_transitions_until_result(self):
        from test_subagents import Child
        owner, rpc = self.authority(agents=[{'name': 'tester', 'description': 'Test', 'tools': 'test'}])
        proposal = Action(id='parent-model', task=self.task.id, tool='__propose__', arguments={})
        owner.gateway.internal(proposal, 0, lambda: Outcome())
        rpc.proposal_id = proposal.id
        owner.child_proposers = {'primary': Child([('run_command', {'command': 'printf checked'})])}
        action, result = self.begin(rpc, agent='tester', background=False)
        self.assertNotIn('background', result)
        child = rpc.children[action.id].effects
        first = rpc.child_model({'permit': action.id, 'route': 'primary'})
        command = rpc.child_tool({'permit': action.id, 'call': first['calls'][0]})
        self.assertEqual(command['outcome']['data']['output'], 'checked')
        write = Action(id='parent-write', task=self.task.id, tool='write_file',
            arguments={'path': 'solution.py', 'text': 'x = 2\n', 'expected_sha256': None})
        rpc.calls = (*rpc.calls, write)
        with self.assertRaises(Conflict):
            rpc.tool(rpc.wire(write))
        native = Action(id='native-write', task=self.task.id, tool='write',
            arguments={'path': 'solution.py', 'content': 'x = 3\n'})
        with self.assertRaises(Conflict):
            rpc.native_begin(rpc.wire(native))
        rpc.plan = SimpleNamespace(execution_mode='parallel')
        with self.assertRaises(Conflict):
            rpc.batch({'proposal_id': rpc.proposal_id})
        rpc.plan = None
        host = self.host(owner)
        host.user_effects = {}
        host.status = lambda: {'subagents': [{'id': action.id, 'background': False, 'status': 'running'}],
            'pending_effect_ids': [key for key, row in self.store.snapshot()['actions'].items() if not row.get('terminal')]}
        host.journal.command('accept', {'input_id': 'user-command', 'text': '!echo blocked',
            'kind': 'prompt', 'channel': 'tui', 'initiator': 'user'})
        with self.assertRaisesRegex(Conflict, 'settled execution boundary'):
            host.snapshot(None, {})
        with self.assertRaisesRegex(Conflict, 'settled foreground execution boundary'):
            host.user_bash(None, {'input_id': 'user-command', 'command': 'echo blocked'})
        self.assertFalse((self.workspace.root / 'solution.py').exists())
        final = rpc.child_model({'permit': action.id, 'route': 'primary'})
        settled = rpc.plugin_settle({'permit': action.id, 'status': 'completed', 'text': final['text']})
        self.assertEqual(settled['outcome']['status'], 'completed')
        self.assertFalse(rpc.children)
        self.assertEqual(rpc.tool(rpc.wire(write))['outcome']['status'], 'completed')
        self.assertEqual((self.workspace.root / 'solution.py').read_text(), 'x = 2\n')

    def test_known_worktree_setup_failure_settles_and_releases_slot(self):
        owner, rpc = self.authority(agents=[{'name': 'worker', 'description': 'Write', 'tools': 'write'}], limit=1)
        first, result = self.begin(rpc, agent='worker')
        self.assertTrue(result['cached'])
        self.assertEqual(result['outcome']['data']['reason'], 'child_setup_failed')
        self.assertTrue(self.store.snapshot()['actions'][first.id]['terminal'])
        self.assertFalse(rpc.children)
        _, result = self.begin(rpc, identity='two')
        self.assertTrue(result['background'])
        rpc.close()

    def test_backgrounds_count_against_host_concurrency(self):
        _, rpc = self.authority(limit=1)
        self.begin(rpc)
        second, result = self.begin(rpc, identity='two')
        self.assertTrue(result['cached'])
        self.assertEqual(result['outcome']['data']['reason'], 'concurrency_limit')
        self.assertEqual(len(rpc.background), 1)
        rpc.close()

    def test_unknown_child_remains_fenced_on_close_and_recovery(self):
        owner, rpc = self.authority()
        action, _ = self.begin(rpc)
        child = rpc.background[action.id].effects
        unknown = Action(id=child.task.id + '/unknown', task=child.task.id, tool='__probe__', arguments={})
        owner.gateway.internal(unknown, 0, lambda: Outcome(status='unknown'))
        rpc.close()
        self.assertNotIn(action.id + '/background', self.store.snapshot()['actions'])
        host = self.host(owner)
        with self.assertRaises(InDoubt):
            recovery_plan(host, cancel_unstarted=True)
        self.assertFalse(host.journal.state()['inputs'])
        # Reconcile the original operation, then recover without redispatching it.
        self.store.settle(unknown, Outcome(data={'original_receipt': True}), 1)
        plan = recovery_plan(host, cancel_unstarted=True)
        self.assertFalse(plan['turns'])
        outcome = self.store.snapshot()['actions'][action.id + '/background']['outcome']
        self.assertEqual(outcome['data']['reason'], 'worker_exited')
        self.assertEqual(len(host.journal.state()['inputs']), 1)

    def test_crash_before_notification_recovers_once(self):
        owner, rpc = self.authority()
        action, _ = self.begin(rpc)
        child = rpc.background[action.id].effects
        child.summary = 'Original final answer.'
        rpc.plugin_settle({'permit': action.id, 'status': 'completed', 'text': child.summary})
        host = self.host(owner)
        reconcile_backgrounds(host)
        first = host.journal.state()['inputs']
        reconcile_backgrounds(host)
        self.assertEqual(host.journal.state()['inputs'], first)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(host.broadcasts), 1)
        self.assertIn('Original final answer.', next(iter(first.values()))['text'])

    def test_crashed_idle_background_is_interrupted_without_new_child(self):
        owner, rpc = self.authority()
        action, _ = self.begin(rpc)
        host = self.host(owner)
        before = set(self.store.snapshot()['tasks'])
        reconcile_backgrounds(host)
        self.assertEqual(set(self.store.snapshot()['tasks']), before)
        result = self.store.snapshot()['actions'][action.id + '/background']['outcome']
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['data']['reason'], 'worker_exited')

    def test_completion_journal_failure_keeps_live_permit(self):
        owner, rpc = self.authority()
        action, _ = self.begin(rpc)
        with patch.object(owner.gateway, 'internal', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                rpc._finish_background(action.id, Outcome())
        self.assertIn(action.id, rpc.background)
        self.assertFalse(rpc.background[action.id].closed)
        rpc.close()

    def test_stop_command_settles_original_receipt_before_close(self):
        owner, rpc = self.authority(agents=[{'name': 'tester', 'description': 'Test', 'tools': 'test'}])
        action, _ = self.begin(rpc, agent='tester', background=False)
        child = rpc.children[action.id].effects
        marker = self.workspace.root / 'started'
        command = f'echo "$HOME" > {shlex.quote(str(marker))}; sleep 300'
        call = Action(id=child.task.id + '/command', task=child.task.id, tool='run_command', arguments={'command': command})
        child.calls = (call,)
        results = []
        thread = Thread(target=lambda: results.append(child.tool(child.wire(call))))
        thread.start()
        self.wait_for(marker.exists)
        owner.stopped_children.add(action.id)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]['outcome']['data']['cancelled'])
        self.assertFalse(Path(marker.read_text().strip()).exists())
        rpc.plugin_settle({'permit': action.id, 'status': 'cancelled', 'text': ''})
        rows = self.store.snapshot()['actions']
        self.assertTrue(rows[call.id]['terminal'])
        self.assertEqual(rows[call.id]['outcome'], results[0]['outcome'])
        self.assertEqual(rows[action.id]['outcome']['status'], 'failed')

    def test_close_cancels_running_command_before_worktree_cleanup(self):
        self.init_project()
        owner, rpc = self.authority(agents=[{'name': 'worker', 'description': 'Write', 'tools': 'write'}])
        with patch.dict(os.environ, {'KERNELBRIDGE_HOME': str(self.base / 'home')}):
            action, _ = self.begin(rpc, agent='worker')
        child = rpc.background[action.id].effects
        marker = child.worktree.path / 'started'
        call = Action(id=child.task.id + '/command', task=child.task.id, tool='run_command',
            arguments={'command': f'touch {shlex.quote(str(marker))}; sleep 300'})
        child.calls = (call,)
        thread = Thread(target=lambda: child.tool(child.wire(call)))
        thread.start()
        self.wait_for(marker.exists)
        rpc.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        rows = self.store.snapshot()['actions']
        self.assertTrue(rows[call.id]['outcome']['data']['cancelled'])
        result = rows[action.id + '/background']['outcome']['data']
        self.assertEqual(result['reason'], 'worker_exited')
        self.assertTrue(result['worktree']['changed'])
        self.assertFalse(child.worktree.path.exists())
        self.assertEqual(git(self.workspace.root, 'show', result['worktree']['branch'] + ':started').stdout, '')

    def test_recovery_retains_worker_commit_and_cleans_checkout(self):
        self.init_project()
        owner, rpc = self.authority(agents=[{'name': 'worker', 'description': 'Write', 'tools': 'write'}])
        with patch.dict(os.environ, {'KERNELBRIDGE_HOME': str(self.base / 'home')}):
            action, _ = self.begin(rpc, agent='worker')
        child = rpc.background[action.id].effects
        (child.worktree.path / 'solution.py').write_text('retained = True\n')
        host = self.host(owner)
        reconcile_backgrounds(host)
        result = self.store.snapshot()['actions'][action.id + '/background']['outcome']['data']
        self.assertTrue(result['worktree']['checkout_removed'])
        self.assertEqual(git(self.workspace.root, 'show', result['worktree']['branch'] + ':solution.py').stdout, 'retained = True\n')
        self.assertFalse(child.worktree.path.exists())

    def test_unknown_worker_never_cleans_live_checkout(self):
        self.init_project()
        owner, rpc = self.authority(agents=[{'name': 'worker', 'description': 'Write', 'tools': 'write'}])
        with patch.dict(os.environ, {'KERNELBRIDGE_HOME': str(self.base / 'home')}):
            action, _ = self.begin(rpc, agent='worker')
        child = rpc.background[action.id].effects
        pending = Action(id=child.task.id + '/unresolved', task=child.task.id, tool='__probe__', arguments={})
        owner.gateway.internal(pending, 0, lambda: Outcome(status='unknown'))
        rpc.close()
        self.assertTrue(child.worktree.path.exists())
        self.assertNotIn(action.id + '/background', self.store.snapshot()['actions'])
        host = self.host(owner)
        with self.assertRaises(InDoubt):
            recovery_plan(host, cancel_unstarted=True)
        self.assertTrue(child.worktree.path.exists())

    def test_original_journal_records_real_admission_and_settlement_times(self):
        _, rpc = self.authority()
        action, _ = self.begin(rpc)
        rpc.close()
        events = list(self.store._events(self.store.events()))
        start = next(e['time_ns'] for e in events if e['kind'] == 'admitted' and e['action']['id'] == action.id)
        end = next(e['time_ns'] for e in events if e['kind'] == 'settled' and e['action_id'] == action.id + '/background')
        self.assertGreaterEqual(end, start)
        self.assertGreater(start, 0)

    def prepared_parent(self, *, concurrent=True):
        owner, rpc = self.authority()
        owner.concurrent_delegation = concurrent
        owner.proposers = {'primary': SimpleNamespace(propose=lambda context, tools:
            AssistantTurn(content=(TextContent(text='Original response.'),), stop_reason='stop'))}
        rpc.active = False
        rpc.hello()
        prepared = rpc.prepare_turn()
        child_task = Task(id='test/background/leaf', scope=self.task.scope, goal='Read in parallel')
        self.store.register_task(child_task.model_dump(mode='json'))
        return owner, rpc, prepared['context_source'], child_task

    def test_parallel_spending_does_not_reject_observed_parent_context(self):
        owner, rpc, context, child_task = self.prepared_parent()
        original_budget = dict(context['budget'])
        owner.gateway.internal(Action(id='child-charge', task=child_task.id, tool='__probe__', arguments={}),
            1, lambda: Outcome())
        result = rpc.model({'route': 'primary', 'context': context})
        self.assertEqual(result['text'], 'Original response.')
        self.assertEqual(context['budget'], original_budget)
        self.assertEqual(self.store.snapshot()['spent'], 2)
        receipt = self.store.snapshot()['actions'][rpc.proposal_id]
        self.assertEqual(receipt['action']['arguments']['context']['budget'], original_budget)

    def test_parallel_spending_still_enforces_actual_atomic_limit(self):
        owner, rpc, context, child_task = self.prepared_parent()
        owner.gateway.internal(Action(id='child-charge', task=child_task.id, tool='__probe__', arguments={}),
            300, lambda: Outcome())
        result = rpc.model({'route': 'primary', 'context': context})
        self.assertEqual(result['result']['status'], 'budget_exhausted')
        self.assertEqual(self.store.snapshot()['spent'], 300)
        self.assertFalse(any(row['action']['tool'] == '__propose__' for row in self.store.snapshot()['actions'].values()))

    def test_parallel_context_cannot_forge_observed_budget(self):
        _, rpc, context, _ = self.prepared_parent()
        context['budget']['spent'] += 1
        with self.assertRaises(Conflict):
            rpc.model({'route': 'primary', 'context': context})

    def test_nonconcurrent_context_keeps_exact_budget_check(self):
        owner, rpc, context, child_task = self.prepared_parent(concurrent=False)
        owner.gateway.internal(Action(id='child-charge', task=child_task.id, tool='__probe__', arguments={}),
            1, lambda: Outcome())
        with self.assertRaisesRegex(Conflict, 'budget changed before admission'):
            rpc.model({'route': 'primary', 'context': context})

    def test_bounded_output_and_home_removed(self):
        command = f'{shlex.quote(sys.executable)} -c '+shlex.quote('import os; print(os.environ["HOME"]); print("x" * 2000000); print("TAIL")')
        result = run_command_tool(self.workspace.root).execute(Action(id='cmd', task='test', tool='run_command', arguments={'command': command}))
        self.assertTrue(result.data['truncated'])
        self.assertLessEqual(len(result.data['output']), 65537)
        self.assertTrue(result.data['output'].endswith('TAIL\n'))

    def init_project(self):
        project = self.workspace.root
        git(project, 'init', '-q')
        git(project, 'add', '.')
        git(project, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')

    def test_child_commit_retained_even_without_staged_changes(self):
        self.init_project()
        project = self.workspace.root
        tree = Worktree(project, 'child-with-commit', self.base / 'home')
        (tree.path / 'solution.py').write_text('x = 2\n')
        git(tree.path, 'add', '.')
        git(tree.path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'child commit')
        result = tree.finish('finish')
        self.assertTrue(result['changed'])
        self.assertTrue(result['checkout_removed'])
        self.assertFalse(tree.path.exists())
        self.assertEqual(git(project, 'show', result['branch'] + ':solution.py').stdout, 'x = 2\n')
        self.assertFalse((project / 'solution.py').exists())

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(condition())


if __name__ == '__main__':
    unittest.main()
