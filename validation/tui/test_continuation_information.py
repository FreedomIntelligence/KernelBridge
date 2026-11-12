"""Information and waiting boundaries use settled receipts, never wrapper traffic."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import Action, Conflict, Outcome, digest
from kernelcat_executor.state.continuation_progress import continuation_activity, progress_signal
from kernelcat_worker.application_host import ApplicationHost
import test_goal_progress as journal_support
import test_goal_continuation as host_support


def signal(identity='original-job', fingerprint='a' * 64, state='progress', **extra):
    return {**dict(version=1, identity=identity, fingerprint=fingerprint, state=state), **extra}


def observed(seq, key, data=None, *, status='completed', input_id='current', task='task', tool='read', arguments=None):
    return [dict(kind='admitted', seq=seq, action=dict(id=key, task=task, tool=tool, arguments=arguments or {}),
                 application_context=dict(input_id=input_id, goal_id='g')),
            dict(kind='settled', seq=seq + 1, action_id=key, outcome=dict(status=status, data=data or {}))]


RECEIPT = dict(input_id='current', kind='policy', status='completed', accepted_seq=10, applied_seq=11, completed_seq=30)


def activity(rows, receipt=RECEIPT):
    return continuation_activity(rows, receipt, task_id='task', goal_id='g', start_seq=0)


class InformationTests(unittest.TestCase):
    def test_admission_alone_is_not_progress(self):
        self.assertFalse(activity(observed(12, 'r')[:1])['progress'])
        result = activity(observed(12, 'u', status='unknown'))
        self.assertFalse(result['progress'])
        self.assertTrue(result['waiting'])
        self.assertEqual(result['receipt_refs'], [{'tool': 'read_receipt', 'arguments': {'action_id': 'u'}}])

    def test_new_compiler_error_is_information_but_repeat_is_not(self):
        old = observed(2, 'old', {'error': 'missing symbol X'}, status='failed', input_id='previous')
        same = observed(12, 'same', {'error': 'missing symbol X'}, status='failed')
        self.assertFalse(activity(old + same)['progress'])
        self.assertTrue(activity(old + observed(12, 'new', {'error': 'missing symbol Y'}, status='failed'))['progress'])

    def test_receipt_trailer_and_elapsed_time_do_not_fabricate_native_progress(self):
        def native(key, text, elapsed):
            return {'adapter': 'pi@1', 'native_declaration_digest': 'd', 'elapsed_ms': elapsed,
                    'execution': {'action_id': key}, 'result': {'content': [
                        {'type': 'text', 'text': text}, {'type': 'text', 'text': 'Execution receipt: ' + key}]}}
        old = observed(2, 'old', native('old', 'same evidence', 1), input_id='previous')
        self.assertFalse(activity(old + observed(12, 'next', native('next', 'same evidence', 99)))['progress'])
        self.assertTrue(activity(old + observed(12, 'next', native('next', 'changed evidence', 99)))['progress'])

    def test_retry_derived_tool_ids_keep_original_input_scope(self):
        from kernelcat_executor.operations.recovery import _successor
        retry = _successor('task/step/7/model/provider')
        rows = observed(12, retry, {'error': 'HTTP 503'}, status='failed', tool='__propose__')
        rows += observed(15, _successor(retry), {'content': []}, tool='__propose__')
        self.assertFalse(activity(rows)['has_tool_actions'])
        rows += observed(18, _successor(retry) + '/call/0', {'text': 'new evidence'})
        value = activity(rows)
        self.assertTrue(value['progress'])
        self.assertEqual(value['new_action_ids'], [_successor(retry) + '/call/0'])
        # Retrying a model does not make another input's tool this input's work.
        rows[-2]['application_context']['input_id'] = 'other-input'
        self.assertFalse(activity(rows)['progress'])

    def test_semantic_identity_ignores_wrapper_noise_but_new_page_counts(self):
        old = observed(2, 'old', {'run': 'wrapper1', 'continuation_progress': signal()}, input_id='previous')
        same = observed(12, 'next', {'run': 'wrapper2', 'continuation_progress': signal()})
        self.assertFalse(activity(old + same)['progress'])
        page = observed(12, 'page2', {'continuation_progress': signal(fingerprint='b' * 64)})
        self.assertTrue(activity(old + page)['progress'])

    def test_first_waiting_blocks_even_with_new_information(self):
        result = activity(observed(12, 'wait', {'continuation_progress': signal(state='waiting')}))
        self.assertTrue(result['progress'])
        self.assertTrue(result['waiting'])

    def test_unrelated_evidence_and_identical_status_cannot_clear_waiting(self):
        rows = observed(12, 'wait', {'continuation_progress': signal(state='waiting')})
        rows += observed(15, 'page', {'continuation_progress': signal(identity='artifact-page')})
        rows += observed(18, 'same', {'continuation_progress': signal()})
        self.assertTrue(activity(rows)['waiting'])
        rows += observed(21, 'terminal', {'continuation_progress': signal(fingerprint='b' * 64)})
        self.assertFalse(activity(rows)['waiting'])

    def test_future_input_child_and_post_completion_never_count(self):
        rows = observed(12, 'future', input_id='future') + observed(15, 'child', task='task/child')
        rows += observed(30, 'late')  # settlement after completed_seq
        rows += observed(32, 'later')
        self.assertFalse(activity(rows)['progress'])

    def test_old_action_settling_inside_input_is_neither_current_nor_prior_information(self):
        old = observed(8, 'old', {'text': 'newly observed'}, input_id='previous')
        old[1]['seq'] = 13
        rows = old + observed(16, 'current', {'text': 'newly observed'})
        rows.sort(key=lambda row: row['seq'])
        self.assertEqual(activity(rows)['new_action_ids'], ['current'])
        self.assertFalse(activity(old)['progress'])

    def test_tool_action_presence_is_scoped_to_current_foreground_input(self):
        excluded = observed(2, 'old', input_id='previous')
        excluded += observed(12, 'child', task='task/child')
        excluded += observed(15, 'future', input_id='future')
        excluded += observed(18, 'bookkeeping', tool='get_goal')
        excluded += observed(32, 'after-completion')
        self.assertFalse(activity(excluded)['has_tool_actions'])
        # Admission is enough to distinguish the public tool-free reason, but
        # is never enough to establish settled information progress.
        value = activity(excluded + observed(21, 'admitted-only')[:1])
        self.assertTrue(value['has_tool_actions'])
        self.assertFalse(value['progress'])

    def test_no_completed_boundary_cannot_count_progress(self):
        self.assertFalse(activity(observed(12, 'r'), dict(accepted_seq=10))['progress'])

    def test_malformed_signal_never_falls_back_to_wrapper_noise(self):
        bad = signal(fingerprint='BAD')
        result = activity(observed(12, 'bad', {'run': 'new-wrapper', 'continuation_progress': bad}))
        self.assertFalse(result['progress'])
        self.assertTrue(result['invalid_signal'])
        self.assertEqual(result['receipt_refs'][0]['arguments']['action_id'], 'bad')

    def test_explicit_null_signal_rejects_wrapper_noise_but_legacy_absence_still_works(self):
        old = observed(2, 'old', {'run': 'wrapper1', 'error': 'same damaged source', 'continuation_progress': None},
                       status='failed', input_id='previous')
        current = observed(12, 'current', {'run': 'wrapper2', 'error': 'same damaged source', 'continuation_progress': None},
                           status='failed')
        result = activity(old + current)
        self.assertFalse(result['progress'])
        self.assertTrue(result['invalid_signal'])
        self.assertEqual(result['receipt_refs'], [{'tool': 'read_receipt', 'arguments': {'action_id': 'current'}}])
        # Legacy ordinary tools do not claim the semantic protocol and retain
        # their settled-output fallback, including useful new failure detail.
        legacy = activity(observed(12, 'legacy', {'error': 'new concrete compiler error'}, status='failed'))
        self.assertTrue(legacy['progress'])
        self.assertFalse(legacy['invalid_signal'])

    def test_signal_bounds_use_utf8_bytes_and_detach_references(self):
        valid = signal(identity='字' * 682, summary='字' * 1365,
                       receipt_refs=[{'tool': 'read', 'arguments': {'path': 'original'}}])
        copied = progress_signal(valid)
        self.assertIsNotNone(copied)
        copied['receipt_refs'][0]['arguments']['path'] = 'mutated'
        self.assertEqual(valid['receipt_refs'][0]['arguments']['path'], 'original')
        invalid = [signal(state={}), signal(version=True), signal(identity='字' * 683), signal(summary='字' * 1366),
                   signal(extra='unrecognized'), signal(receipt_refs=valid['receipt_refs'] * 5),
                   signal(receipt_refs=[{'tool': 'x' * 129, 'arguments': {}}]),
                   signal(receipt_refs=[{'tool': 'read', 'arguments': {'path': 'x' * 4096}}]),
                   signal(receipt_refs=[{'tool': 'read', 'arguments': {}, 'auto_execute': True}])]
        for value in invalid:
            with self.subTest(value=str(value)[:80]):
                self.assertIsNone(progress_signal(value))


class HostBoundaryTests(unittest.TestCase):
    def host(self, receipt, rows, **extra):
        return host_support.NextInputTests().host('task', host_support.state(inputs={'current': receipt}, **extra), rows)

    def test_initial_prompt_waits_on_first_original_receipt_without_accepting_policy(self):
        receipt = dict(RECEIPT, kind='prompt')
        refs = [{'tool': 'pipeline_run', 'arguments': {'operation': 'collect', 'recover_run': 'original'}}]
        rows = observed(12, 'waiting', {'continuation_progress': signal(state='waiting', receipt_refs=refs)})
        host = self.host(receipt, rows)
        result = host.next_input(SimpleNamespace(worker={'worker_id': 'w'}))
        self.assertEqual(result['reason'], 'awaiting_original_receipt')
        self.assertEqual(result['receipt_refs'], refs)
        self.assertEqual([method for method, _ in host.commands], ['goal_outcome'])
        self.assertEqual(host.commands[0][1]['expected_input_id'], 'current')

    def test_repeated_settled_tools_use_new_reason_and_tool_free_keeps_public_reason(self):
        for rows, expected in [([], 'no_tool_progress'),
                (observed(5, 'old', {'error': 'same compiler failure'}, status='failed', input_id='previous')
                 + observed(12, 'repeat', {'error': 'same compiler failure'}, status='failed'), 'no_executable_progress')]:
            with self.subTest(expected=expected):
                host = self.host(RECEIPT, rows)
                result = host.next_input(SimpleNamespace(worker={'worker_id': 'w'}))
                self.assertEqual((result['action'], result['reason']), ('wait', expected))
                self.assertEqual(host.commands[0][1]['status'], 'awaiting_user')
                self.assertEqual([method for method, _ in host.commands], ['goal_outcome'])

    def test_pending_effects_remain_pending_and_do_not_get_marked_awaiting(self):
        host = self.host(RECEIPT, observed(12, 'unknown', status='unknown'), pending_effect_ids=['unknown'])
        result = host.next_input(SimpleNamespace(worker={'worker_id': 'w'}))
        self.assertEqual(result['action'], 'wait')
        self.assertEqual(host.commands, [])

    def test_explicit_resume_skips_the_previous_completed_input(self):
        host = self.host(RECEIPT, [], goal={**host_support.UNLIMITED, 'resume_seq': 31})
        self.assertEqual(host.next_input(SimpleNamespace(worker={'worker_id': 'w'}))['action'], 'continue')

    def test_retrying_applied_input_is_not_completed_no_progress(self):
        from kernelcat_executor.operations.recovery import _successor
        retry = _successor('task/step/7/model/provider')
        current = {key: value for key, value in RECEIPT.items() if key != 'completed_seq'}
        current['status'] = 'applied'
        host = self.host(current, observed(12, retry, {'error': 'HTTP 503'}, status='failed', tool='__propose__'))
        result = host.next_input(SimpleNamespace(worker={'worker_id': 'w'}))
        self.assertEqual((result['action'], result['reason']), ('wait', 'accepted_input'))
        self.assertEqual(host.commands, [])

    def test_ordinary_project_without_goal_never_auto_continues(self):
        host = host_support.NextInputTests().host('project', host_support.state(goal=None))
        self.assertEqual(host.next_input(SimpleNamespace(worker={'worker_id': 'w'}))['action'], 'wait')
        self.assertEqual(host.commands, [])


class GoalAwaitCASTests(unittest.TestCase):
    setUp = journal_support.GoalProgressTests.setUp
    tearDown = journal_support.GoalProgressTests.tearDown
    control = journal_support.GoalProgressTests.control

    def complete_input(self, identity):
        self.app.command('accept', {'input_id': identity, 'text': 'work', 'kind': 'prompt', 'channel': 'sdk', 'initiator': 'user'})
        self.app.command('transition', {**self.lease, 'input_id': identity, 'status': 'applied', 'entry_id': identity, 'pi_session_id': 'fixture'})
        self.app.command('transition', {**self.lease, 'input_id': identity, 'status': 'completed'})

    def awaiting(self):
        return dict(status='awaiting_user', reason='awaiting_original_receipt',
                    expected_goal_sha256=digest(self.app.state()['goal']), expected_input_id='original')

    def test_boundary_accepts_wait_without_touching_original_receipts(self):
        self.control('set', objective='work'); self.complete_input('original')
        inputs = copy.deepcopy(self.app.state()['inputs'])
        result = self.app.command('goal_outcome', self.awaiting())
        self.assertEqual(result['goal']['status'], 'awaiting_user')
        self.assertEqual(result['inputs'], inputs)
        self.assertNotIn('expected_input_id', result['goal'])

    def test_new_queued_applied_and_completed_input_each_defeat_stale_wait(self):
        for status in ('queued', 'applied', 'completed'):
            with self.subTest(status=status):
                self.control('set', objective='work'); self.complete_input('original-' + status)
                receipt = self.awaiting(); receipt['expected_input_id'] = 'original-' + status
                identity = 'new-' + status
                self.app.command('accept', {'input_id': identity, 'text': 'new evidence', 'kind': 'prompt', 'channel': 'sdk', 'initiator': 'user'})
                if status != 'queued':
                    self.app.command('transition', {**self.lease, 'input_id': identity, 'status': 'applied', 'entry_id': identity, 'pi_session_id': 'fixture'})
                if status == 'completed':
                    self.app.command('transition', {**self.lease, 'input_id': identity, 'status': 'completed'})
                before = len(self.store.events())
                self.assertEqual(self.app.command('goal_outcome', receipt)['goal']['status'], 'active')
                self.assertEqual(len(self.store.events()), before)
                if status == 'queued':
                    self.app.command('transition', {**self.lease, 'input_id': identity, 'status': 'applied', 'entry_id': identity, 'pi_session_id': 'fixture'})
                if status != 'completed':
                    self.app.command('transition', {**self.lease, 'input_id': identity, 'status': 'completed'})

    def test_pause_resume_and_new_goal_each_defeat_stale_wait(self):
        self.control('set', objective='work'); self.complete_input('original')
        for operation in ('pause', 'resume', 'set'):
            receipt = self.awaiting()
            self.control(operation, **({'objective': 'new'} if operation == 'set' else {}))
            before = self.app.state()['goal']
            self.assertEqual(self.app.command('goal_outcome', receipt)['goal'], before)

    def test_isolated_blocked_tool_cannot_complete_or_replace_unknown_effects(self):
        self.control('set', objective='work')
        host = ApplicationHost.__new__(ApplicationHost)
        host.config = SimpleNamespace(workspace_mode='task')
        host.journal = self.app
        host.owner = SimpleNamespace(gateway=SimpleNamespace(store=self.store))
        action = Action(id='blocked', task='task', tool='update_goal', arguments={'status': 'blocked', 'summary': 'Need original receipt job-1'})
        self.store.admit(action, 0, self.store.policy()); self.store.started(action)
        unknown = Action(id='unknown-effect', task='task', tool='external')
        self.store.admit(unknown, 0, self.store.policy()); self.store.started(unknown)
        self.store.settle(unknown, Outcome(status='unknown', data={'handle': 'job-1'}), 0)
        before = copy.deepcopy(self.store.snapshot()['actions'][unknown.id])
        result = host.update_codegen_goal(action)
        self.assertEqual(result.data['status'], 'awaiting_user')
        self.assertEqual(self.store.snapshot()['actions'][unknown.id], before)
        with self.assertRaisesRegex(Conflict, 'original acceptance'):
            host.update_codegen_goal(Action(id='claim', task='task', tool='update_goal', arguments={'status': 'complete', 'summary': 'done'}))

    def provider_failure(self, identity):
        self.app.command('accept', {'input_id': identity, 'text': 'continue', 'kind': 'policy', 'channel': 'policy', 'initiator': 'loopx'})
        self.app.command('transition', {**self.lease, 'input_id': identity, 'status': 'applied',
                                       'entry_id': identity, 'pi_session_id': 'fixture'})
        self.app.command('transition', {**self.lease, 'input_id': identity, 'status': 'failed',
                                       'reason': 'providers_failed', 'summary': 'HTTP 503 after bounded retries'})
        host = ApplicationHost.__new__(ApplicationHost)
        host.config = SimpleNamespace(workspace_mode='project', goal_review=SimpleNamespace(enabled=False))
        host.journal = self.app
        host.owner = SimpleNamespace(gateway=SimpleNamespace(store=self.store))
        def status():
            value = self.app.state()
            return host_support.state(goal=value['goal'], inputs=value['inputs'], task=value['task'])
        host.status = status
        return host

    def test_provider_outage_waits_at_its_original_failed_boundary(self):
        self.control('set', objective='work')
        host = self.provider_failure('outage')
        before = copy.deepcopy(self.app.state()['inputs'])
        result = host.next_input(SimpleNamespace(worker=self.lease))
        self.assertEqual(result, {'action': 'wait', 'reason': 'provider_unavailable'})
        self.assertEqual(self.app.state()['goal']['status'], 'awaiting_user')
        self.assertEqual(self.app.state()['inputs'], before)
        self.assertNotIn('expected_input_id', self.app.state()['goal'])

    def test_provider_outage_cannot_override_pause_resume_or_replaced_goal(self):
        for operation in ('pause', 'resume', 'set'):
            with self.subTest(operation=operation):
                self.control('set', objective='original')
                host = self.provider_failure('outage-' + operation)
                command = self.app.command
                raced = []
                def interleave(method, data):
                    if method == 'goal_outcome' and not raced:
                        raced.append(True)
                        self.control(operation, **({'objective': 'replacement'} if operation == 'set' else {}))
                    return command(method, data)
                self.app.command = interleave
                try:
                    result = host.next_input(SimpleNamespace(worker=self.lease))
                finally:
                    self.app.command = command
                self.assertEqual(result['reason'], 'new_input_boundary')
                current = self.app.state()['goal']
                self.assertEqual(current['status'], 'paused' if operation == 'pause' else 'active')
                self.assertNotEqual(current.get('reason'), 'provider_unavailable')
                if operation == 'set': self.assertEqual(current['objective'], 'replacement')

    def test_provider_outage_cannot_override_new_user_input_or_later_terminal(self):
        for status in ('queued', 'applied', 'completed', 'failed', 'interrupted'):
            with self.subTest(status=status):
                self.control('set', objective='work')
                host = self.provider_failure('outage-' + status)
                command = self.app.command
                identity = 'new-' + status
                raced = []
                def interleave(method, data):
                    if method == 'goal_outcome' and not raced:
                        raced.append(True)
                        command('accept', {'input_id': identity, 'text': 'new input', 'kind': 'prompt', 'channel': 'sdk', 'initiator': 'user'})
                        if status != 'queued':
                            command('transition', {**self.lease, 'input_id': identity, 'status': 'applied',
                                                   'entry_id': identity, 'pi_session_id': 'fixture'})
                        if status in {'completed', 'failed', 'interrupted'}:
                            command('transition', {**self.lease, 'input_id': identity, 'status': status})
                    return command(method, data)
                self.app.command = interleave
                try:
                    result = host.next_input(SimpleNamespace(worker=self.lease))
                finally:
                    self.app.command = command
                self.assertEqual(result['reason'], 'new_input_boundary')
                self.assertEqual(self.app.state()['goal']['status'], 'active')
                self.assertEqual(self.app.state()['inputs'][identity]['status'], status)
                if status in {'queued', 'applied'}:
                    command('transition', {**self.lease, 'input_id': identity, 'status': 'interrupted'})

    def test_blocker_admitted_before_pause_resume_is_known_rejected(self):
        self.control('set', objective='work')
        action = Action(id='old-blocker', task='task', tool='update_goal', arguments={'status': 'blocked', 'summary': 'old blocked'})
        self.store.admit(action, 0, self.store.policy())
        host = ApplicationHost.__new__(ApplicationHost); host.journal = self.app
        host.owner = SimpleNamespace(gateway=SimpleNamespace(store=self.store))
        self.control('pause')
        self.assertEqual(host.update_codegen_goal(action).status, 'failed')
        self.assertEqual(self.app.state()['goal']['status'], 'paused')
        self.control('resume')
        self.assertEqual(host.update_codegen_goal(action).status, 'failed')
        self.assertEqual(self.app.state()['goal']['status'], 'active')

    def test_old_blocker_cannot_override_new_goal(self):
        self.control('set', objective='old')
        action = Action(id='old-blocker', task='task', tool='update_goal', arguments={'status': 'blocked', 'summary': 'old blocked'})
        self.store.admit(action, 0, self.store.policy())
        self.control('set', objective='new')
        host = ApplicationHost.__new__(ApplicationHost); host.journal = self.app
        host.owner = SimpleNamespace(gateway=SimpleNamespace(store=self.store))
        result = host.update_codegen_goal(action)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(self.app.state()['goal']['status'], 'active')


if __name__ == '__main__': unittest.main()
