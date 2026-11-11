"""Real community Pi children with host-enforced read, count and budget limits."""
from pathlib import Path
import time
import unittest

import test_runtime as runtime_support
from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_executor.contracts.messages import AssistantTurn, TextContent, ToolCall
from kernelcat_executor.contracts.models import Policy
from kernelcat_worker.configuration import SubagentConfig


class Child:
    def __init__(self, calls=()):
        self.calls = iter(calls); self.count = 0; self.tools = []; self.contexts = []

    def propose(self, context, tools):
        self.count += 1; self.tools.append({t.name for t in tools})
        self.contexts.append(context)
        call = next(self.calls, None)
        return AssistantTurn(content=(ToolCall(id='child-call', name=call[0], arguments=call[1]),)
            if call else (TextContent(text='Original read receipts inspected.'),), stop_reason='tool_calls' if call else 'stop')


class SubagentTests(unittest.TestCase):
    setUp = runtime_support.RuntimeTests.setUp
    tearDown = runtime_support.RuntimeTests.tearDown
    session = runtime_support.RuntimeTests.session

    def run_child(self, calls=(), mode='read_only', settings=None, parent_calls=None):
        config = SubagentConfig(**(settings or {})).model_dump(exclude={'enabled'})
        effect = SubagentEffect(config)
        request = ('delegate', {'agent': 'kernel-scout', 'task': 'Inspect original evidence', 'mode': mode})
        session, provider, replay = self.session(parent_calls or [request], extra_tools=[effect])
        child = Child(calls); session.child_proposers = {'primary': child}
        result = session.run(self.task)
        rows = [r for r in self.store.snapshot()['actions'].values() if r['action']['tool'] == 'delegate']
        replay._boundary()
        return result, rows, child, session

    def test_readonly_child_shared_receipts_and_no_repeat_on_resume(self):
        result, rows, child, session = self.run_child([('read_file', {'path': 'reference/spec.txt'})])
        self.assertEqual(result['status'], 'finished')
        self.assertEqual(rows[0]['outcome']['status'], 'completed')
        self.assertEqual(child.count, 2)
        self.assertTrue(all('read_file' in names and not names & {'write_file', 'bash', 'delegate', 'evaluate'} for names in child.tools))
        actions = self.store.snapshot()['actions']
        self.assertTrue(any('/leaf/' in key and row['action']['tool'] == 'read_file' for key, row in actions.items()))
        spent = self.store.snapshot()['spent']
        session.run(self.task)
        self.assertEqual(self.store.snapshot()['spent'], spent)
        self.assertEqual(child.count, 2)

    def test_analysis_rejects_tools_and_parent_recovers(self):
        result, rows, _, _ = self.run_child([('read_file', {'path': 'reference/spec.txt'})], mode='analysis')
        self.assertEqual(result['status'], 'finished')
        self.assertEqual(rows[0]['outcome']['data']['reason'], 'unexpected_tool')

    def test_discovery_tools_work_in_real_pi_child(self):
        _, rows, child, _ = self.run_child([
            ('list_files', {'path': 'reference', 'pattern': '*.txt'}),
            ('search_text', {'path': 'reference', 'query': 'immutable'}),
            ('read_file', {'path': 'reference/spec.txt'}),
        ])
        self.assertEqual(rows[0]['outcome']['status'], 'completed')
        actions = self.store.snapshot()['actions'].values()
        search = next(row for row in actions if row['action']['tool'] == 'search_text')
        self.assertEqual(search['outcome']['data']['matches'][0]['path'], 'reference/spec.txt')
        self.assertTrue(all({'list_files', 'search_text'} <= names for names in child.tools))

    def test_step_limit_reserves_summary_with_original_evidence(self):
        _, rows, child, _ = self.run_child([('read_file', {'path': 'reference/spec.txt'})], settings={'max_steps': 2})
        data = rows[0]['outcome']['data']
        self.assertEqual(rows[0]['outcome']['status'], 'completed')
        self.assertTrue(data['partial'])
        self.assertEqual(data['completion_reason'], 'step_limit')
        self.assertEqual((data['steps'], data['tool_calls']), (2, 1))
        self.assertEqual(child.tools[-1], set())
        self.assertIn('final summary call', child.contexts[-1]['instruction'])
        self.assertIn('immutable', str(child.contexts[-1]))

    def test_read_limit_reserves_tool_free_summary(self):
        _, rows, child, _ = self.run_child([('read_file', {'path': 'reference/spec.txt'})], settings={'tool_limit': 1})
        self.assertEqual(rows[0]['outcome']['status'], 'completed')
        self.assertEqual(rows[0]['outcome']['data']['completion_reason'], 'tool_limit')
        self.assertEqual(child.tools[-1], set())

    def test_default_readonly_and_delegation_policy(self):
        config = SubagentConfig()
        self.assertEqual(config.policy, 'auto')
        # Like Claude Code and Codex: no per-child step/read/time caps by default.
        self.assertEqual((config.max_steps, config.tool_limit, config.timeout, config.max_per_turn), (None, None, None, None))
        self.assertEqual(config.max_concurrent, 6)
        effect = SubagentEffect(config.model_dump())
        self.assertIn('Use proactively', effect.spec.description)
        self.assertEqual(effect.spec.parameters['properties']['mode']['default'], 'read_only')
        self.assertEqual(effect.spec.parameters['properties']['description']['maxLength'], 80)
        self.assertNotIn('description', effect.spec.parameters['required'])
        explicit = SubagentEffect(SubagentConfig(policy='explicit').model_dump())
        self.assertIn('explicitly request', explicit.spec.description)
        _, rows, child, _ = self.run_child([('read_file', {'path': 'reference/spec.txt'})],
            parent_calls=[('delegate', {'agent': 'kernel-scout', 'task': 'Inspect the reference'})])
        self.assertEqual(rows[0]['outcome']['data']['mode'], 'read_only')
        self.assertEqual(child.count, 2)

    def test_delegate_only_reply_records_independent_delegation_batch(self):
        effect = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        calls = [('delegate', {'agent': 'kernel-scout', 'description': 'A', 'task': 'Inspect A'}),
                 ('delegate', {'agent': 'kernel-reviewer', 'description': 'B', 'task': 'Inspect B'})]
        session, _, replay = self.session([calls], extra_tools=[effect])
        session.concurrent_delegation = True
        child = Child([('read_file', {'path': 'reference/spec.txt'})]); session.child_proposers = {'primary': child}
        self.assertEqual(session.run(self.task)['status'], 'finished')
        actions = self.store.snapshot()['actions']
        batch = next(row for key, row in actions.items() if key.endswith('/batch'))
        self.assertEqual(batch['outcome']['data']['execution_mode'], 'delegation')
        delegates = sorted((row for row in actions.values() if row['action']['tool'] == 'delegate'), key=lambda row: row['action']['id'])
        self.assertEqual([row['outcome']['status'] for row in delegates], ['completed', 'completed'])
        proposal = delegates[0]['action']['parents'][0]
        self.assertTrue(all(tuple(row['action']['parents']) == (proposal, proposal + '/batch') for row in delegates))
        spent = self.store.snapshot()['spent']
        session.run(self.task)
        self.assertEqual(self.store.snapshot()['spent'], spent)
        replay._boundary()

    def test_mixed_reply_keeps_source_order_chain(self):
        effect = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        calls = [('read_file', {'path': 'reference/spec.txt'}),
                 ('delegate', {'agent': 'kernel-scout', 'task': 'Inspect A'})]
        session, _, replay = self.session([calls], extra_tools=[effect])
        session.concurrent_delegation = True
        session.child_proposers = {'primary': Child()}
        session.run(self.task)
        self.assertFalse(any(key.endswith('/batch') for key in self.store.snapshot()['actions']))
        replay._boundary()

    def run_typed(self, agent, calls, extra=None):
        import os
        effect = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        effect.add_agents([{'name': agent, 'tools': extra or 'read', 'description': 'fixture'}])
        session, _, replay = self.session([('delegate', {'agent': agent, 'description': 'typed', 'task': 'Typed fixture'})], extra_tools=[effect])
        child = Child(calls); session.child_proposers = {'primary': child}
        result = session.run(self.task)
        rows = [r for r in self.store.snapshot()['actions'].values() if r['action']['tool'] == 'delegate']
        replay._boundary()
        return result, rows, child

    def test_tester_runs_commands_without_host_credentials(self):
        import os
        os.environ['KERNELBRIDGE_FIXTURE_SECRET'] = 'must-not-leak'
        try:
            _, rows, child = self.run_typed('fixture-tester', [('run_command', {'command': 'echo ok-$((1+2)); echo "[$KERNELBRIDGE_FIXTURE_SECRET]"; exit 3'})], 'test')
        finally:
            del os.environ['KERNELBRIDGE_FIXTURE_SECRET']
        self.assertEqual(rows[0]['outcome']['status'], 'completed')
        self.assertTrue(all('run_command' in names and 'write_file' not in names for names in child.tools[:1]))
        command = next(r for r in self.store.snapshot()['actions'].values() if r['action']['tool'] == 'run_command')
        self.assertEqual(command['outcome']['data']['exit_code'], 3)
        self.assertIn('ok-3', command['outcome']['data']['output'])
        self.assertIn('[]', command['outcome']['data']['output'])

    def test_worker_edits_only_its_worktree_branch(self):
        import subprocess, os
        root = self.workspace.root
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
        (root / 'solution.py').write_text('x = 1\n')
        subprocess.run(['git', '-C', str(root), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base'], check=True)
        os.environ['KERNELBRIDGE_HOME'] = str(self.base / 'kbhome')
        try:
            _, rows, child = self.run_typed('fixture-worker', [('write_file', {'path': 'notes.md', 'text': 'worker change\n', 'expected_sha256': None}),
                                                               ('run_command', {'command': 'cat notes.md'})], 'write')
        finally:
            del os.environ['KERNELBRIDGE_HOME']
        data = rows[0]['outcome']['data']
        self.assertFalse((root / 'notes.md').exists())
        self.assertTrue(data['worktree']['changed'], data)
        branch = data['worktree']['branch']
        shown = subprocess.run(['git', '-C', str(root), 'show', f'{branch}:notes.md'], capture_output=True, text=True, check=True).stdout
        self.assertEqual(shown, 'worker change\n')
        self.assertIn('notes.md', data['worktree']['diffstat'])
        command = next(r for r in self.store.snapshot()['actions'].values() if r['action']['tool'] == 'run_command')
        self.assertIn('worker change', command['outcome']['data']['output'])

    def test_definition_files_follow_claude_code_format(self):
        from kernelcat_worker.subagent_definitions import discover, tools_class
        project, home = self.base / 'defs-project', self.base / 'defs-home'
        (project / '.kernelbridge/agents').mkdir(parents=True); (home / 'agents').mkdir(parents=True)
        (home / 'agents/docs.md').write_text('---\nname: docs-writer\ndescription: user version\n---\nUser prompt')
        (project / '.kernelbridge/agents/docs.md').write_text('---\nname: docs-writer\ndescription: project version\ntools: Read, Grep, Bash\nmodel: primary\n---\nProject prompt')
        (project / '.kernelbridge/agents/bad.md').write_text('no frontmatter')
        agents = {agent['name']: agent for agent in discover(project, home, project_mode=True)}
        self.assertEqual(agents['docs-writer']['description'], 'project version')
        self.assertEqual((agents['docs-writer']['tools'], agents['docs-writer']['source'], agents['docs-writer']['model']), ('test', 'project', 'primary'))
        self.assertIn('kernel-worker', agents); self.assertNotIn('bad', agents)
        isolated = {agent['name'] for agent in discover(project, home, project_mode=False)}
        self.assertNotIn('kernel-worker', isolated); self.assertNotIn('docs-writer', isolated)
        self.assertEqual([tools_class(v) for v in ('Read, Edit', 'Bash', 'Read, Grep', 'write')], ['write', 'test', 'read', 'write'])

    def test_forged_write_never_changes_workspace(self):
        result, rows, _, _ = self.run_child([('write_file', {'path': 'solution.py', 'text': 'bad'})])
        self.assertFalse((self.workspace.root / 'solution.py').exists())
        denied = [r for r in self.store.snapshot()['actions'].values() if r['action']['tool'] == 'write_file']
        self.assertEqual(denied[0]['outcome']['status'], 'failed')
        self.assertEqual(result['status'], 'finished')

    def test_step_and_tool_limits_settle_failed(self):
        _, rows, child, _ = self.run_child([('read_file', {'path': 'reference/spec.txt'})], settings={'max_steps': 1})
        self.assertEqual(rows[0]['outcome']['data']['reason'], 'step_limit')
        self.assertEqual(child.count, 1)

    def test_zero_child_tool_limit_prevents_read(self):
        _, rows, _, _ = self.run_child([('read_file', {'path': 'reference/spec.txt'})], settings={'tool_limit': 0})
        self.assertEqual(rows[0]['outcome']['data']['reason'], 'tool_limit')
        self.assertFalse(any(r['action']['tool'] == 'read_file' for r in self.store.snapshot()['actions'].values()))

    def test_total_limit_includes_prior_parent_turns(self):
        call = ('delegate', {'agent': 'kernel-scout', 'task': 'Explicit advisory analysis'})
        _, rows, child, _ = self.run_child(settings={'max_total': 1}, parent_calls=[call, call])
        self.assertEqual([r['outcome']['status'] for r in rows], ['completed', 'failed'])
        self.assertEqual(child.count, 1)

    def test_configuration_limits_and_disabled_binding(self):
        from pydantic import ValidationError
        for values in ({'policy': 'always'}, {'max_concurrent': 0}, {'max_per_turn': 0}, {'max_total': 0}, {'max_steps': 0}, {'tool_limit': -1}, {'timeout': 0}):
            with self.assertRaises(ValidationError): SubagentConfig(**values)
        # Disabled configuration is also exercised at the runtime tool registry
        # boundary by the binding test; a missing delegate cannot launch a child.
        session, provider, replay = self.session([('delegate', {'agent': 'kernel-scout', 'task': 'Unavailable'})])
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertTrue(all('delegate' not in names for names in provider.declarations))
        replay._boundary()

    def test_child_spends_the_parent_budget(self):
        from kernelcat_executor.profiles.store import AgentJournal
        self.store = AgentJournal(self.base / 'small/events.jsonl')
        self.store.initialize(4, Policy(max_steps=25, context_bytes=262144))
        result, rows, child, _ = self.run_child(mode='analysis')
        self.assertEqual(self.store.snapshot()['spent'], 4)
        self.assertEqual(rows[0]['outcome']['status'], 'completed')
        self.assertEqual(child.count, 1)
        self.assertNotEqual(result['status'], 'finished')

    def test_deadline_settles_child_without_success(self):
        class SlowChild(Child):
            def propose(self, context, tools):
                time.sleep(1.2)
                return super().propose(context, tools)
        effect = SubagentEffect(SubagentConfig(timeout=1).model_dump(exclude={'enabled'}))
        session, _, _ = self.session([('delegate', {'agent': 'kernel-scout', 'task': 'Slow fixture', 'mode': 'read_only'})], extra_tools=[effect])
        session.child_proposers = {'primary': SlowChild([('read_file', {'path': 'reference/spec.txt'})])}
        session.run(self.task)
        rows = [r for r in self.store.snapshot()['actions'].values() if r['action']['tool'] == 'delegate']
        self.assertNotEqual(rows[0].get('outcome', {}).get('status'), 'completed')
        self.assertFalse(any(r['action']['tool'] == 'read_file' for r in self.store.snapshot()['actions'].values()))

    def test_interrupt_never_reports_child_success_or_runs_pending_read(self):
        class CancellingChild(Child):
            def propose(child, context, tools):
                session.cancel()
                return super().propose(context, tools)
        effect = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        session, _, _ = self.session([('delegate', {'agent': 'kernel-scout', 'task': 'Cancel fixture', 'mode': 'read_only'})], extra_tools=[effect])
        session.child_proposers = {'primary': CancellingChild([('read_file', {'path': 'reference/spec.txt'})])}
        result = session.run(self.task)
        self.assertEqual(result['status'], 'cancelled')
        rows = [r for r in self.store.snapshot()['actions'].values() if r['action']['tool'] == 'delegate']
        self.assertNotEqual(rows[0].get('outcome', {}).get('status'), 'completed')
        self.assertFalse(any(r['action']['tool'] == 'read_file' for r in self.store.snapshot()['actions'].values()))

    def test_unknown_child_receipt_cannot_be_settled_as_failure_or_replayed(self):
        from kernelcat_executor.capabilities.worker import WorkerError
        from kernelcat_executor.contracts.models import InDoubt
        class UnknownChild(Child):
            def propose(child, context, tools):
                child.count += 1
                raise TimeoutError('fixture lost response after dispatch')
        effect = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        session, _, _ = self.session([('delegate', {'agent': 'kernel-scout', 'task': 'Uncertain fixture'})], extra_tools=[effect])
        child = UnknownChild(); session.child_proposers = {'primary': child}
        with self.assertRaises((WorkerError, InDoubt)):
            session.run(self.task)
        actions = self.store.snapshot()['actions'].values()
        rows = [r for r in actions if r['action']['tool'] == 'delegate' or '/leaf/' in r['action']['id']]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['outcome']['status'] == 'unknown' for r in rows))
        with self.assertRaises((WorkerError, InDoubt)):
            session.run(self.task)
        self.assertEqual(child.count, 1)


if __name__ == '__main__': unittest.main(verbosity=2)
