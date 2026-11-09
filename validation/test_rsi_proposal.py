"""A hallucinated citation must fail before creating an unrecoverable proposal."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for path in ('source/packages/executor/src', 'source/apps/worker/src', 'source/packages/knowledge/src'):
    sys.path.insert(0, str(ROOT / path))
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.rsi_artifacts import source_files
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.models import Action, Outcome, Policy, Task
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.state.journal import Journal
from kernelcat_worker.rsi_experiments import ExperimentTool
from kernelcat_worker.rsi_records import Records


class ProposalValidation(unittest.TestCase):
    def initialize_baseline(self, gateway, records, workspace):
        action, record = records.prepare({'record_id':'task/rsi/initial',
            'expected_sequence':0, 'kind':'initial',
            'value':{'source':source_files(workspace, ['solution.py'])}}, '__rsi_record__')
        gateway.internal(action, 0, lambda:records.outcome(record))

    def test_unknown_citation_then_valid_resubmission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Journal(root / 'events.jsonl');store.initialize(50, Policy())
            store.register_task(Task(id='task', scope='fixture', goal='Test a recoverable proposal error.').model_dump(mode='json'))
            workspace = Workspace(root / 'workspace')
            workspace.root.mkdir(exist_ok=True)
            (workspace.root / 'solution.py').write_text('candidate')
            artifacts = RuntimeArtifacts(root / 'artifacts')
            tool = ExperimentTool(store, workspace, artifacts, {'source_paths':['solution.py']})
            gateway = Gateway(store, [tool], 'fixture')
            self.initialize_baseline(gateway, Records(store, artifacts, 'task'), workspace)
            (workspace.root / 'solution.py').write_text('changed candidate')
            args = {'hypothesis':'test vectorization', 'expected_delta':0.1, 'cites':['invented']}
            rejected = gateway.execute(Action(id='task/bad', task='task', tool='run_experiment', arguments=args))
            self.assertEqual(rejected.status, 'failed')
            self.assertFalse(rejected.data['submitted'])
            self.assertEqual(len(Records(store, artifacts, 'task').entries()), 1)
            accepted = gateway.execute(Action(id='task/good', task='task', tool='run_experiment', arguments={**args,'cites':[]}))
            self.assertEqual(accepted.status, 'completed')
            self.assertEqual(len(Records(store, artifacts, 'task').entries()), 2)

    def test_noop_then_edit_submits_without_poisoning_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Journal(root / 'events.jsonl');store.initialize(50, Policy())
            store.register_task(Task(id='task', scope='fixture', goal='Recover from an unchanged proposal.').model_dump(mode='json'))
            workspace = Workspace(root / 'workspace')
            (workspace.root / 'solution.py').write_text('baseline')
            artifacts = RuntimeArtifacts(root / 'artifacts')
            records = Records(store, artifacts, 'task')
            gateway = Gateway(store, [ExperimentTool(store, workspace, artifacts,
                {'source_paths':['solution.py']})], 'fixture')
            self.initialize_baseline(gateway, records, workspace)
            args = {'hypothesis':'use multiple cores', 'expected_delta':0.1, 'cites':[]}
            action = Action(id='task/noop', task='task', tool='run_experiment', arguments=args)
            rejected = gateway.execute(action)
            self.assertEqual(rejected.status, 'failed')
            self.assertEqual(rejected.data['reason'], 'unchanged_source')
            self.assertEqual(len(records.entries()), 1)
            self.assertEqual(gateway.execute(action), rejected)
            (workspace.root / 'solution.py').write_text('changed candidate')
            accepted = gateway.execute(action.model_copy(update={'id':'task/changed'}))
            self.assertEqual(accepted.status, 'completed')
            self.assertEqual(records.latest('proposal')['value']['candidate'],
                             source_files(workspace, ['solution.py']))
            self.assertEqual(len(records.entries()), 2)

    def test_exhausted_confirmation_budget_does_not_create_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Journal(root / 'events.jsonl');store.initialize(50, Policy())
            store.register_task(Task(id='task', scope='fixture', goal='Respect confirmation budget.').model_dump(mode='json'))
            workspace = Workspace(root / 'workspace')
            (workspace.root / 'solution.py').write_text('candidate')
            artifacts = RuntimeArtifacts(root / 'artifacts')
            tool = ExperimentTool(store, workspace, artifacts, {'source_paths':['solution.py'], 'policy':{'holdout_limit':1}})
            gateway = Gateway(store, [tool], 'fixture')
            prior = Action(id='task/confirm', task='task', tool='__rsi_adapter__', arguments={'operation':'confirm'})
            gateway.internal(prior, 0, lambda:Outcome())
            result = gateway.execute(Action(id='task/proposal', task='task', tool='run_experiment',
                arguments={'hypothesis':'another experiment','expected_delta':0.1,'cites':[]}))
            self.assertEqual(result.status, 'failed')
            self.assertEqual(result.data['reason'], 'holdout_limit_exhausted')
            self.assertEqual(Records(store, artifacts, 'task').entries(), [])
