"""Goal completion requires original F/G/H receipts, never a model's success text."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import digest
from kernelcat_worker.application_pipeline_acceptance import collect_pipeline_acceptance, pipeline_hash, promoted_receipt


class OperatorAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        source = {'candidate.py': hashlib.sha256(b'fixture candidate').hexdigest()}
        baseline = {'eager.py': hashlib.sha256(b'fixture comparator').hexdigest()}
        oracle = {'oracle.py': hashlib.sha256(b'fixture independent oracle').hexdigest()}
        contract = {'task': {'operator': 'non-scatter fixture'}, 'oracle': {'files': oracle},
                    'suite': [{'id': 'all', 'family': 'full'}], 'measurement': {'scope': 'protocol_fixture'}}
        self.declared = copy.deepcopy({'kind': 'operator_pipeline', 'tool': 'pipeline_promote', 'evidence_tier': 'E3',
                         'pipeline_contract': contract, 'baseline_files': baseline, 'source_paths': ['candidate.py'], 'scope': 'protocol_fixture'})
        files = [{'path': 'candidate.py', 'sha256': source['candidate.py'], 'text': 'fixture candidate', 'mode': 0o644}]
        source_sha = digest(files)
        snapshots = self.root / 'checkpoints'; snapshots.mkdir()
        (snapshots / (source_sha + '.json')).write_text(json.dumps({'source_sha256': source_sha, 'files': files}))
        binding = {'source': pipeline_hash(source), 'baseline': pipeline_hash(baseline), 'contract': 'frozen-contract', 'revision': 1}
        full = {'checks': dict.fromkeys(('build', 'semantics', 'full'), True), 'case_count': 1, 'case_ids': ['all'],
                'oracle_sha256': pipeline_hash(oracle), 'scope': 'protocol_fixture'}
        raw = [{'source': binding['source'], 'contract': binding['contract'], 'result': full}]
        for files in (baseline, source, source, baseline):
            raw.append({'source': pipeline_hash(files), 'contract': binding['contract'], 'result': {'correct': True,
                'measurement': contract['measurement'], 'environment': {'device': 'fixture'}, 'cases': {'all': [100, 101, 99]}}})
        paths = []
        for index, value in enumerate(raw):
            path = self.root / f'receipt-{index}.json'; path.write_text(json.dumps(value)); paths.append(str(path))
        check = {'passed': True, 'scope': 'protocol_fixture', 'binding': binding, 'receipt': paths[0], 'action_id': 'f'}
        pair = {'binding': binding, 'action_id': 'g', 'receipts': paths[1:], 'environment': {'device': 'fixture'},
                'speedup': 1.2, 'cases': {'all': 1.2}, 'families': {'full': 1.2}, 'regressions': [],
                'qualification': {'qualified': True, 'paired_delta': .18, 'noise_threshold': .02,
                                  'method': 'original_baseline_repeat_drift', 'scope': 'protocol_fixture'}}
        evidence = {'promotion_action_id': 'h', 'contract': contract, 'source_files': source, 'baseline_files': baseline,
                    'binding': binding, 'check': check, 'pair': pair,
                    'receipts': {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}}
        def action(name, tool, result, arguments=None):
            return {'action': {'id': name, 'task': 'task', 'tool': tool, 'arguments': arguments or {}},
                    'terminal': True, 'outcome': {'status': 'completed', 'data': result}}
        self.actions = {'f': action('f', 'pipeline_run', {'result': raw[0]}, {'stage': 'F'}),
                        'g': action('g', 'pipeline_pair', {'result': copy.deepcopy(pair)}),
                        'h': action('h', 'pipeline_promote', {'promotion_evidence': evidence})}
        self.contexts = {key: {'task_version': 1, 'evaluator_sha256': 'pinned-evaluator', 'source_sha256': source_sha} for key in self.actions}
        self.state = {'task_version': 1, 'evidence': [], 'goal': {'status': 'active'}}
        self.commands = []
        events = [{'kind': 'admitted', 'action': action['action'], 'application_context': self.contexts[key]} for key, action in self.actions.items()]
        store = SimpleNamespace(path=self.root / 'events.jsonl', events=lambda: events, _events=lambda values: values,
                                snapshot=lambda: {'actions': self.actions})
        self.host = SimpleNamespace(task=SimpleNamespace(id='task'), owner=SimpleNamespace(gateway=SimpleNamespace(store=store)),
            evaluator={'sha256': 'pinned-evaluator', 'files': {}}, config=SimpleNamespace(application=SimpleNamespace(complete_on_acceptance=True)),
            journal=SimpleNamespace(command=lambda name, value: self.commands.append((name, value))))

    def tearDown(self): self.temp.cleanup()

    def receipt(self):
        return promoted_receipt(self.host, self.state, self.declared, 'h', self.actions['h'], self.actions, self.contexts)

    def test_original_full_pair_promotes_e1_and_e3_then_completes(self):
        collect_pipeline_acceptance(self.host, self.state, self.declared)
        self.assertEqual([name for name, _ in self.commands], ['evidence', 'evidence', 'goal_outcome'])
        self.assertEqual([value['evidence_tier'] for name, value in self.commands if name == 'evidence'], ['E1', 'E3'])
        self.assertEqual(self.commands[-1][1], {'status': 'completed', 'evidence_ref': 'h'})
        self.assertEqual(self.commands[1][1]['raw_data_ref'][0], str(self.root / 'receipt-0.json'))

    def test_completion_switch_retains_evidence_without_finishing_goal(self):
        self.host.config.application.complete_on_acceptance = False
        collect_pipeline_acceptance(self.host, self.state, self.declared)
        self.assertEqual([name for name, _ in self.commands], ['evidence', 'evidence'])

    def test_changed_baseline_or_contract_cannot_qualify(self):
        self.declared['baseline_files']['eager.py'] = '0' * 64
        self.assertIsNone(self.receipt())
        self.declared['baseline_files'] = self.actions['h']['outcome']['data']['promotion_evidence']['baseline_files']
        self.declared['pipeline_contract'] = copy.deepcopy(self.declared['pipeline_contract'])
        self.declared['pipeline_contract']['measurement']['repeats'] = 1
        self.assertIsNone(self.receipt())

    def test_current_source_and_original_g_must_match(self):
        self.contexts['g']['source_sha256'] = 'different-snapshot'
        self.assertIsNone(self.receipt())
        self.contexts['g']['source_sha256'] = self.contexts['h']['source_sha256']
        self.actions['g']['terminal'] = False
        self.assertIsNone(self.receipt())

    def test_raw_receipt_tampering_and_unqualified_gain_are_rejected(self):
        pair = self.actions['h']['outcome']['data']['promotion_evidence']['pair']
        pair['qualification']['qualified'] = False
        self.actions['g']['outcome']['data']['result'] = copy.deepcopy(pair)
        self.assertIsNone(self.receipt())
        pair['qualification']['qualified'] = True
        self.actions['g']['outcome']['data']['result'] = copy.deepcopy(pair)
        (self.root / 'receipt-2.json').write_text('{}')
        self.assertIsNone(self.receipt())

    def test_truncated_f_and_g_recover_only_action_bound_hashed_results(self):
        evidence = self.actions['h']['outcome']['data']['promotion_evidence']
        old_paths = [evidence['check']['receipt'], *evidence['pair']['receipts']]
        new_paths = []
        for index, old in enumerate(old_paths):
            identity = 'f' if index == 0 else 'g'
            folder = self.root / hashlib.sha256(identity.encode()).hexdigest()
            path = folder / ('driver' if index == 0 else f'arm-{index - 1}') / 'receipt.json'
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(Path(old).read_bytes())
            new_paths.append(str(path))
        evidence['check']['receipt'] = new_paths[0]
        evidence['pair']['receipts'] = new_paths[1:]
        evidence['receipts'] = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in new_paths}
        evidence['pair']['large_diagnostic_summary'] = 'x' * 20000
        self.actions['g']['outcome']['data']['result'] = copy.deepcopy(evidence['pair'])
        for identity in ('f', 'g'):
            folder = self.root / hashlib.sha256(identity.encode()).hexdigest()
            result = folder / 'result.json'
            result.write_text(json.dumps({'status': 'completed', 'data': self.actions[identity]['outcome']['data']['result']}))
            manifest = folder / 'manifest.json'
            manifest.write_text(json.dumps({'result.json': hashlib.sha256(result.read_bytes()).hexdigest()}))
            self.actions[identity]['outcome']['data'] = {'result': 'Read result.json with pipeline_read',
                'run': folder.name, 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}
        self.assertIsNotNone(self.receipt())
        # A new file under the same run ID cannot replace the admitted result.
        result.write_text('{}')
        self.assertIsNone(self.receipt())


if __name__ == '__main__': unittest.main()
