"""Trial reports retain original accounting and reject altered Journal bytes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('trial_audit', ROOT / 'source/scripts/audit_operator_trial.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AuditTests(unittest.TestCase):
    def test_atomic_role_accounting_diagnostics_and_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'state').mkdir(); path = root / 'state/events.jsonl'
            def admitted(identity, task, tool, arguments=None):
                return {'kind': 'admitted', 'action': {'id': identity, 'task': task,
                    'tool': tool, 'arguments': arguments or {}}}
            def settled(identity, data):
                return {'kind': 'settled', 'action_id': identity, 'outcome': {'status': 'completed', 'data': data}}
            entries = [admitted('parent', 'task', '__propose__'),
                {'kind': 'atomic', 'events': [settled('parent', {'usage': {'total_tokens': 12}}),
                    admitted('child', 'task/leaf', '__propose__')]},
                settled('child', {}), admitted('task/compact/1', 'task', '__propose__'),
                admitted('failed-model', 'task', '__propose__'),
                {'kind': 'settled', 'action_id': 'failed-model', 'outcome': {'status': 'failed',
                    'data': {'error': 'malformed provider response', 'failure_details': {
                        'category': 'protocol', 'usage': {'prompt_tokens': 13, 'completion_tokens': 4}}}}},
                admitted('j', 'task', 'pipeline_run', {'stage': 'J', 'driver': 'ir'}),
                settled('j', {'run': 'original', 'result': {'status': 'unavailable'}}),
                admitted('read', 'task', 'pipeline_read', {'run': 'original', 'path': 'compiler.log'}),
                settled('read', {'text': 'unsupported export'})]
            previous = '0' * 64; rows = []
            for seq, entry in enumerate(entries):
                row = dict(entry, seq=seq, prev=previous)
                previous = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':'),
                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()
                rows.append(dict(row, digest=previous))
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            result = module.audit(root)
            self.assertEqual(result['model_turns'], 4)
            self.assertEqual(result['tokens'], 29)
            self.assertEqual(result['by_role']['parent']['usage_unknown_calls'], 0)
            self.assertEqual(result['by_role']['child']['usage_unknown_calls'], 1)
            self.assertEqual(result['by_role']['compact']['pending_calls'], 1)
            self.assertEqual(len(result['explicit_diagnostic_calls']), 1)
            self.assertEqual(result['artifact_reads'][0]['path'], 'compiler.log')
            self.assertFalse(result['accepted_e3'])
            path.write_text(path.read_text().replace('total_tokens": 12', 'total_tokens": 99'))
            with self.assertRaisesRegex(ValueError, 'hash chain'):
                module.audit(root)


if __name__ == '__main__':
    unittest.main()
