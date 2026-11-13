"""Opt-in blocker review against real Journal receipts; no external model/device."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_goal_progress as support  # Establish the repository Python paths.
from test_goal_review import Reviewer
from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, Outcome, Policy, Task, ToolFailure, digest
from kernelcat_executor.capabilities.task_instructions import TaskInstructionFile, admitted_task_instructions
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.state.application import ApplicationJournal
from kernelcat_executor.state.goal_progress import goal_usage
from kernelcat_executor.state.journal import Journal
from kernelcat_worker.application_host import ApplicationHost
from kernelcat_worker.configuration import GoalReviewConfig


class CountedReviewer(Reviewer):
    def __init__(self, response):
        super().__init__(response)
        self.invocations = 0
        self.before_prepare = lambda: None

    def prepare_harness(self, harness):
        self.before_prepare()
        return super().prepare_harness(harness)

    def invoke_harness(self, request):
        self.invocations += 1
        return super().invoke_harness(request)


class GoalBlockerReviewTests(unittest.TestCase):
    control = support.GoalProgressTests.control

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='kb-blocker-review-')
        self.case = 0
        self.setup_case()

    def tearDown(self):
        self.directory.cleanup()

    def setup_case(self, *, mode='project', enabled=True, response=None, objective='Read the authorized fixture',
                   limits=None, goal=True, goal_id=None, seed_files=None):
        self.case += 1
        self.store = Journal(Path(self.directory.name) / f'{self.case}.jsonl')
        self.store.initialize(None, Policy(context_bytes=262144))
        task = Task(id='task', goal='Read the authorized fixture', scope='test',
                    contract={'requirement': 'Do not modify the protected evaluator'},
                    seed_files=seed_files if seed_files is not None else {
                        'AGENTS.md': 'Read original evidence. Only candidate/ is writable.',
                        'api.md': 'Use the original supported interface.'})
        self.store.register_task(task.model_dump(mode='json'))
        self.app = ApplicationJournal(self.store, 'app')
        self.app.command('create', {'task': task.model_dump(mode='json'), 'workspace_mode': mode,
            'goal': None, 'goal_defaults': limits or {}})
        self.lease = self.app.command('acquire', {'worker_id': 'worker', 'mode': 'headless'})['lease']
        self.store.bind_application_lease('app', 'worker', self.lease['lease_epoch'])
        if goal:
            if goal_id:
                self.accept(goal_id, kind='control')
                self.app.command('control', {'input_id': goal_id, 'operation': 'set', 'objective': objective, **(limits or {})})
            else:
                self.control('set', objective=objective, **(limits or {}))
        self.provider = CountedReviewer(response or {'status': 'continue', 'summary': 'Read the original API before declaring it unavailable.'})
        self.host = ApplicationHost.__new__(ApplicationHost)
        self.host.identity = 'app'
        self.host.task = Task.model_validate(self.app.state()['task'])
        self.host.journal = self.app
        self.host.config = SimpleNamespace(workspace_mode=mode, goal_review=GoalReviewConfig(enabled=enabled))
        self.host.owner = SimpleNamespace(gateway=Gateway(self.store, [], 'test'),
            proposers={'primary': self.provider}, workspace_policy={
                'writable_paths': ['candidate'], 'readonly_paths': ['evaluator'], 'codemode': 'bounded'})
        self.host.status = self.status
        self.connection = SimpleNamespace(worker=self.lease)

    def status(self):
        state, execution = self.app.state(), self.store.snapshot()
        state['calls'] = {key: item for key, item in state['calls'].items() if item['status'] == 'pending'}
        state.update(budget={key: execution.get(key) for key in ('limit', 'spent', 'reserved')},
            pending_effect_ids=[key for key, item in execution['actions'].items() if not item.get('terminal')],
            model_turns=sum(item['action']['tool'] == '__propose__' for item in execution['actions'].values()),
            max_trial_turns=None, plan=None)
        if state['goal']:
            state['goal_usage'] = goal_usage(list(self.store._events(self.store.events())), state['goal'])
        return state

    def accept(self, key, *, kind='prompt'):
        return self.app.command('accept', {'input_id': key, 'text': key, 'kind': kind,
            'channel': 'sdk', 'initiator': 'user'})

    def apply(self, key):
        self.app.command('transition', {**self.lease, 'input_id': key, 'status': 'applied',
            'entry_id': key, 'pi_session_id': 'fixture'})
        state = self.app.state()
        self.store.bind_application_context({'input_id': key, 'goal_id': (state['goal'] or {}).get('goal_id'),
            'task_version': state['task_version']})

    def finish(self, key):
        self.app.command('transition', {**self.lease, 'input_id': key, 'status': 'completed'})

    def tool(self, key, data, *, status='completed'):
        action = Action(id=key, task='task', tool='read_file', arguments={'path': 'api.md'})
        self.store.admit(action, 0, self.store.policy()); self.store.started(action)
        self.store.settle(action, Outcome(status=status, data=data), 0)

    def block(self, key='work', *, accept=True, complete=True, observation=None):
        if accept:
            self.accept(key)
        self.apply(key)
        if observation is not None:
            self.tool(key + '/read', observation)
        action = Action(id=key + '/blocked', task='task', tool='update_goal',
                        arguments={'status': 'blocked', 'summary': 'The required API seems unavailable.'})
        self.store.admit(action, 0, self.store.policy()); self.store.started(action)
        outcome = self.host.update_codegen_goal(action)
        self.store.settle(action, outcome, 0)
        if complete:
            self.finish(key)
        return outcome

    def next(self):
        return self.host.next_input(self.connection)

    def reviews(self):
        return {key: item for key, item in self.store.snapshot()['actions'].items() if '/goal-blocker/goal-review/' in key}

    def take_over(self):
        self.app.command('release', {**self.lease, 'on_detach': 'continue'})
        self.lease = self.app.command('acquire', {'worker_id': 'replacement', 'mode': 'headless', 'recovering': True})['lease']

    def rebind(self):
        self.connection.worker = self.lease
        self.store.bind_application_lease('app', self.lease['worker_id'], self.lease['lease_epoch'])

    def test_disabled_and_no_goal_do_not_call_reviewer(self):
        self.assertFalse(GoalReviewConfig().enabled)
        self.setup_case(enabled=False)
        self.assertEqual(self.block().data['status'], 'awaiting_user')
        self.next()
        self.assertEqual(self.provider.invocations, 0)
        self.setup_case(goal=False)
        self.assertEqual(self.next()['reason'], 'goal_inactive')
        self.assertEqual(self.provider.invocations, 0)

    def test_proposal_stays_active_until_input_is_settled_in_both_modes(self):
        for mode in ('project', 'isolated'):
            with self.subTest(mode=mode):
                self.setup_case(mode=mode)
                proposal = self.block(complete=False)
                self.assertTrue(proposal.data['review_pending'])
                self.assertEqual(self.app.state()['goal']['status'], 'active')
                self.assertTrue(self.host.goal_blocker_pending(self.app.state()['goal'], 'work'))
                self.assertEqual(self.next()['reason'], 'accepted_input')
                self.assertEqual(self.provider.invocations, 0)
                self.finish('work')
                self.assertEqual(self.next()['reason'], 'reviewed_blocker')

    def test_fresh_review_sees_real_authority_and_charges_original_budget(self):
        self.block(observation={'text': 'Original API page'})
        result = self.next()
        facts = json.loads(self.provider.requests[0]['messages'][0]['content'])
        self.assertEqual(self.provider.requests[0]['tools'], [])
        self.assertEqual(facts['workspace_authority']['readonly_paths'], ['evaluator'])
        self.assertIn('seed_files/AGENTS.md', {item['name'] for item in facts['frozen_task_records']})
        self.assertIn('contract', {item['name'] for item in facts['frozen_task_records']})
        self.assertEqual(facts['registered_tool_names'], ['read_receipt'])
        self.assertIn('not proof', facts['registered_tools_boundary'])
        self.assertTrue(any(item.get('outcome', {}).get('data', {}).get('text') == 'Original API page' for item in facts['evidence']))
        self.assertEqual(self.store.snapshot()['spent'], 1)
        self.assertEqual(self.status()['goal_usage']['by_role']['review']['model_turns'], 1)
        self.assertIn('decision.blocker_review', result['receipt']['text'])
        self.assertEqual(self.next()['receipt']['input_id'], result['receipt']['input_id'])
        self.assertEqual(self.provider.invocations, 1)

    def test_project_review_uses_frozen_root_instructions_without_rereading_files(self):
        self.setup_case(seed_files={})
        parent = Path(self.directory.name)
        root = parent / 'project'; root.mkdir()
        (root / 'nested').mkdir()
        (parent / 'AGENTS.md').write_text('Unadmitted ancestor instruction')
        (root / 'nested/AGENTS.md').write_text('Unadmitted nested instruction')
        text = '原始授权指导\r\nUse supported tools; evaluator is read-only.\n'
        (root / 'AGENTS.md').write_bytes(text.encode('utf-8'))
        frozen = admitted_task_instructions(self.host.task, workspace_mode='project',
                                           readonly_paths=(), workspace_root=root)
        self.host.owner.instruction_files = frozen.files
        (root / 'AGENTS.md').write_text('Changed after host admission; must not reach review')
        self.block(); self.next()
        facts = json.loads(self.provider.requests[0]['messages'][0]['content'])
        expected = {'path': 'AGENTS.md', 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'bytes': len(text.encode())}
        self.assertTrue(facts['instruction_snapshot_available'])
        self.assertEqual(facts['instruction_manifest'], [expected])
        self.assertEqual(facts['frozen_instruction_records'], [{**expected, 'text': text}])
        self.assertEqual(facts['omitted_instruction_records'], 0)
        self.assertNotIn('Unadmitted', json.dumps(facts))
        self.assertNotIn('Changed after host admission', json.dumps(facts))

    def test_isolated_admitted_instruction_scope_is_preserved_and_seed_not_duplicated(self):
        text = 'The admitted root instructions.'
        self.setup_case(mode='isolated', seed_files={'AGENTS.md': text,
            'AGENTS.task.md': 'A writable seed is not an admitted instruction', 'api.md': 'Original API'})
        frozen = admitted_task_instructions(self.host.task, workspace_mode='isolated', readonly_paths=('AGENTS.md',))
        self.host.owner.instruction_files = frozen.files
        self.block(); self.next()
        facts = json.loads(self.provider.requests[0]['messages'][0]['content'])
        self.assertEqual([item['path'] for item in facts['frozen_instruction_records']], ['AGENTS.md'])
        self.assertEqual(facts['frozen_instruction_records'][0]['text'], text)
        self.assertNotIn('seed_files/AGENTS.md', {item['name'] for item in facts['frozen_task_records']})
        self.assertEqual(facts['workspace_authority']['readonly_paths'], ['evaluator'])
        self.assertEqual(facts['omitted_task_records'], 0)

    def test_instruction_overflow_retains_raw_identity_and_explicit_omission(self):
        text = 'Instruction outside the review context budget.\n' * 1000
        self.setup_case(mode='isolated', seed_files={'AGENTS.md': text})
        instruction = TaskInstructionFile('AGENTS.md', text)
        self.host.owner.instruction_files = (instruction,)
        self.host.config.goal_review = GoalReviewConfig(enabled=True, context_bytes=8192)
        self.block(); self.next()
        request = self.provider.requests[0]
        facts = json.loads(request['messages'][0]['content'])
        self.assertEqual(facts['frozen_instruction_records'], [])
        self.assertEqual(facts['omitted_instruction_records'], 1)
        self.assertEqual(facts['instruction_manifest'], [{'path': 'AGENTS.md',
            'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())}])
        self.assertNotIn('seed_files/AGENTS.md', {item['name'] for item in facts['frozen_task_records']})
        from kernelcat_executor.contracts.models import canonical
        self.assertLessEqual(len(canonical(request).encode()), 8192)

    def test_missing_and_known_empty_instruction_snapshots_remain_compatible(self):
        for snapshot in (None, ()):
            with self.subTest(snapshot=snapshot):
                self.setup_case()
                self.host.owner.instruction_files = snapshot
                self.block()
                self.assertEqual(self.next()['reason'], 'reviewed_blocker')
                facts = json.loads(self.provider.requests[0]['messages'][0]['content'])
                self.assertEqual(facts['instruction_snapshot_available'], snapshot is not None)
                self.assertEqual(facts['instruction_manifest'], [])
                self.assertEqual(facts['frozen_instruction_records'], [])
                self.assertEqual(facts['omitted_instruction_records'], 0)
                self.assertIn('seed_files/AGENTS.md', {item['name'] for item in facts['frozen_task_records']})

    def test_distinct_frozen_instruction_and_seed_bytes_are_not_conflated(self):
        self.host.owner.instruction_files = (TaskInstructionFile('AGENTS.md', 'Host-frozen project instructions'),)
        self.block(); self.next()
        facts = json.loads(self.provider.requests[0]['messages'][0]['content'])
        self.assertEqual(facts['frozen_instruction_records'][0]['text'], 'Host-frozen project instructions')
        seed = next(item for item in facts['frozen_task_records'] if item['name'] == 'seed_files/AGENTS.md')
        self.assertEqual(seed['value'], self.host.task.seed_files['AGENTS.md'])

    def test_reviewed_continuation_without_new_information_stops_once(self):
        self.block()
        first = self.next()['receipt']
        self.block(first['input_id'], accept=False)
        self.assertEqual(self.next()['reason'], 'blocker_no_new_information')
        self.assertEqual(self.provider.invocations, 1)
        self.assertEqual(self.app.state()['goal']['status'], 'awaiting_user')

    def test_new_settled_tool_information_allows_a_new_review(self):
        self.block()
        first = self.next()['receipt']
        self.block(first['input_id'], accept=False, observation={'text': 'New API evidence'})
        self.assertEqual(self.next()['reason'], 'reviewed_blocker')
        self.assertEqual(self.provider.invocations, 2)

    def test_crash_before_accept_reuses_original_review_after_new_lease(self):
        self.block()
        command = self.app.command
        def crash(method, data):
            if method == 'accept' and 'goal_blocker_review' in data:
                raise RuntimeError('crash before continuation persistence')
            return command(method, data)
        with patch.object(self.app, 'command', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                self.next()
        self.take_over()
        self.rebind()
        result = self.next()
        self.assertEqual(result['reason'], 'reviewed_blocker')
        self.assertEqual(self.provider.invocations, 1)
        self.assertEqual(len(self.reviews()), 1)

    def test_crash_after_accept_returns_original_queued_input(self):
        self.block()
        command = self.app.command
        def crash(method, data):
            result = command(method, data)
            if method == 'accept' and 'goal_blocker_review' in data:
                raise RuntimeError('lost continuation acknowledgement')
            return result
        with patch.object(self.app, 'command', side_effect=crash):
            with self.assertRaises(RuntimeError): self.next()
        first = self.next()
        self.assertEqual(first['action'], 'input')
        self.assertEqual(self.next()['receipt']['input_id'], first['receipt']['input_id'])
        self.assertEqual(self.provider.invocations, 1)

    def test_crash_before_blocked_outcome_reapplies_same_review_receipt(self):
        self.setup_case(response={'status': 'blocked', 'summary': 'The original required credential is unavailable.'})
        self.block()
        command = self.app.command
        def crash(method, data):
            if method == 'goal_outcome': raise RuntimeError('crash before applying verdict')
            return command(method, data)
        with patch.object(self.app, 'command', side_effect=crash):
            with self.assertRaises(RuntimeError): self.next()
        self.assertEqual(self.app.state()['goal']['status'], 'active')
        self.assertEqual(self.next()['reason'], 'review_blocked')
        self.assertEqual(self.provider.invocations, 1)

    def test_verdict_failure_met_and_blocked_cannot_finish_isolated_goal(self):
        cases = [({'status': 'met', 'summary': 'done'}, 'blocker_review_failed'),
                 ({'status': 'continue', 'summary': ''}, 'blocker_review_failed'),
                 ({'status': 'blocked', 'summary': 'Original permission explicitly excludes this operation.'}, 'review_blocked')]
        for response, reason in cases:
            with self.subTest(response=response):
                self.setup_case(mode='isolated', response=response)
                self.block()
                self.assertEqual(self.next()['reason'], reason)
                self.assertEqual(self.app.state()['goal']['status'], 'awaiting_user')
                self.assertIsNone(self.app.state()['best'])
                self.assertEqual(self.app.state()['evidence'], [])

    def test_blocker_purpose_cannot_authorize_completion_even_goal_named_blocker(self):
        self.setup_case(goal_id='blocker', response={'status': 'met', 'summary': 'done'})
        self.block(); self.next()
        review_id = next(iter(self.reviews()))
        with self.assertRaises(Conflict):
            self.app.command('goal_outcome', {'status': 'completed', 'completion_kind': 'review',
                'action_id': review_id, 'summary': 'forged completion'})

    def test_unknown_review_retains_original_receipt_and_never_redispatches(self):
        self.setup_case(response=TimeoutError('unknown original response'))
        self.block()
        with self.assertRaises(InDoubt): self.next()
        self.assertEqual(self.next()['reason'], 'reconciliation')
        self.assertEqual(self.provider.invocations, 1)
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.store.snapshot()['reserved'], 1)

    def test_known_provider_failure_is_not_a_proven_external_blocker(self):
        self.setup_case(response=ToolFailure('provider rejected the review'))
        self.block()
        self.assertEqual(self.next()['reason'], 'blocker_review_failed')
        self.assertEqual(self.app.state()['goal']['reason'], 'blocker_review_failed')
        self.assertEqual(self.provider.invocations, 1)

    def test_original_unknown_and_pending_call_block_review(self):
        self.block(complete=False)
        self.tool('external', {'handle': 'original'}, status='unknown')
        self.finish('work')
        original = self.store.snapshot()['actions']['external']
        self.assertEqual(self.next()['reason'], 'reconciliation')
        self.assertEqual(self.store.snapshot()['actions']['external'], original)
        self.assertEqual(self.provider.invocations, 0)
        self.setup_case(); self.block()
        self.app.command('call_intent', {**self.lease, 'call_id': 'pending', 'method': 'tool', 'params': {}})
        self.assertEqual(self.next()['reason'], 'reconciliation')
        self.assertEqual(self.provider.invocations, 0)

    def test_producer_waiting_is_not_overruled_by_review(self):
        self.block(observation={'continuation_progress': {'version': 1, 'identity': 'original-job',
            'fingerprint': digest('waiting'), 'state': 'waiting'}})
        self.assertEqual(self.next()['reason'], 'awaiting_original_receipt')
        self.assertEqual(self.provider.invocations, 0)

    def test_limit_reached_by_review_prevents_continuation(self):
        self.setup_case(limits={'max_model_turns': 1})
        self.block()
        self.assertEqual(self.next()['reason'], 'budget_limit')
        self.assertEqual(self.provider.invocations, 1)
        self.assertEqual(self.app.state()['goal']['status'], 'budget_limit')
        self.assertFalse(any(item['kind'] == 'policy' for item in self.app.state()['inputs'].values()))

    def test_limit_reached_before_review_does_not_dispatch(self):
        self.setup_case(limits={'token_budget': 1})
        self.block(complete=False)
        action = Action(id='previous-model', task='task', tool='__propose__')
        self.store.admit(action, 1, self.store.policy()); self.store.started(action)
        self.store.settle(action, Outcome(data={'usage': {'total_tokens': 1}}), 0)
        self.finish('work')
        self.assertEqual(self.next()['reason'], 'budget_limit')
        self.assertEqual(self.provider.invocations, 0)

    def test_trial_reset_survives_queued_continuation_replay(self):
        self.block()
        status = self.host.status
        self.host.status = lambda: {**status(), 'max_trial_turns': 1}
        result = self.next()
        self.assertEqual(result['action'], 'reset')
        self.assertEqual(result['reason'], 'trial_limit')
        self.assertEqual(result['receipt']['transition_operation'], 'reset')
        self.assertEqual(self.next()['action'], 'reset')
        self.assertEqual(self.provider.invocations, 1)

    def test_context_overflow_and_continuation_invalid_have_persistent_reasons(self):
        self.setup_case(objective='x' * 80000)
        self.host.config.goal_review = GoalReviewConfig(enabled=True, context_bytes=4096)
        self.block()
        self.assertEqual(self.next()['reason'], 'blocker_review_context')
        self.assertEqual(self.provider.invocations, 0)
        self.setup_case(objective='x' * 66000)
        self.block()
        result = self.next()
        self.assertEqual(result['reason'], 'continuation_input_invalid')
        self.assertIn('65536', result['error_summary'])
        self.assertEqual(self.app.state()['goal']['reason'], 'continuation_input_invalid')
        self.assertEqual(self.provider.invocations, 1)

    def mutate_boundary(self, operation):
        if operation == 'input': self.accept('new-user-input')
        elif operation.startswith('input-'):
            self.accept('new-user-input'); self.apply('new-user-input')
            self.app.command('transition', {**self.lease, 'input_id': 'new-user-input',
                'status': operation.removeprefix('input-')})
        elif operation == 'queued-control': self.accept('new-user-control', kind='control')
        elif operation == 'resume':
            self.control('pause'); self.control('resume')
        elif operation == 'replace': self.control('set', objective='replacement')
        else: self.control(operation)

    def test_new_input_and_controls_win_before_model_admission_without_charge(self):
        for operation in ('input', 'input-completed', 'input-failed', 'input-interrupted', 'queued-control', 'clear', 'pause', 'resume', 'replace'):
            with self.subTest(operation=operation):
                self.setup_case(); self.block()
                self.provider.before_prepare = lambda: self.mutate_boundary(operation)
                result = self.next()
                self.assertEqual(result['reason'], 'review_admission_stopped')
                self.assertEqual(self.provider.invocations, 0)
                self.assertEqual(self.store.snapshot()['spent'], 0)
                self.assertEqual(self.store.snapshot()['reserved'], 0)
                self.assertFalse(self.reviews())

    def test_control_between_reservation_and_start_cancels_only_unstarted_review(self):
        self.block()
        started = self.store.started
        def interleave(action):
            if '/goal-blocker/goal-review/' in action.id: self.control('clear')
            return started(action)
        with patch.object(self.store, 'started', side_effect=interleave):
            self.assertEqual(self.next()['reason'], 'review_admission_stopped')
        self.assertEqual(self.provider.invocations, 0)
        self.assertEqual(self.store.snapshot()['reserved'], 0)
        self.assertEqual(self.store.snapshot()['spent'], 0)
        self.assertTrue(next(iter(self.reviews().values()))['outcome']['data']['cancelled_unstarted'])

    def test_late_verdict_preserves_new_input_and_all_controls(self):
        for operation in ('input', 'input-completed', 'input-failed', 'input-interrupted', 'queued-control', 'clear', 'pause', 'resume', 'replace'):
            with self.subTest(operation=operation):
                self.setup_case(); self.block()
                self.provider.hook = lambda: self.mutate_boundary(operation)
                self.assertEqual(self.next()['reason'], 'new_input_boundary')
                self.assertEqual(self.provider.invocations, 1)
                self.assertFalse(any(item['kind'] == 'policy' for item in self.app.state()['inputs'].values()))

    def test_lease_change_before_admission_never_dispatches(self):
        self.block()
        self.provider.before_prepare = self.take_over
        # The original Journal handle still carries the old worker's lease.
        with self.assertRaises(Conflict): self.next()
        self.assertEqual(self.provider.invocations, 0)
        self.assertFalse(any(item['kind'] == 'policy' for item in self.app.state()['inputs'].values()))

    def test_lease_change_before_accept_requires_original_receipt_recovery(self):
        self.block()
        self.provider.hook = self.take_over
        with self.assertRaises(Conflict): self.next()
        self.assertEqual(self.provider.invocations, 1)
        self.rebind()
        self.assertEqual(self.next()['reason'], 'reviewed_blocker')
        self.assertEqual(self.provider.invocations, 1)


if __name__ == '__main__': unittest.main(verbosity=2)
