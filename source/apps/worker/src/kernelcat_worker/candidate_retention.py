"""Select source only from settled, source-bound business-tool receipts."""
import hashlib
import json

from jsonschema import Draft202012Validator
from kernelcat_executor.capabilities.runtime_artifacts import workspace_bundle
from kernelcat_executor.contracts.models import Conflict
from kernelcat_executor.operations.registry import GateDecision


def field(value, path):
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def qualified_source(store, artifacts, task_id, retention):
    validator = Draft202012Validator(retention.outcome_schema)
    for row in reversed(list(store.snapshot()['actions'].values())):
        outcome = row.get('outcome', {})
        if (row['action']['task'] != task_id or row['action']['tool'] != retention.tool
                or not row.get('terminal') or outcome.get('status') != 'completed'
                or not validator.is_valid(outcome)):
            continue
        reference = outcome.get('data', {}).get('captured_source')
        if reference is None:
            continue
        source = json.loads(artifacts.read(reference))
        if len(source) != len(retention.source_hashes) or {f['path'] for f in source} != set(retention.source_hashes):
            raise Conflict('captured source paths differ from the retention policy')
        if all(hashlib.sha256(f['text'].encode()).hexdigest() == field(outcome, retention.source_hashes[f['path']]) for f in source):
            return source
    return None


def retained_business_bundle(workspace, original, *, store, artifacts, task_id, retention):
    source = qualified_source(store, artifacts, task_id, retention)
    return workspace_bundle(workspace, original, source_overrides=source)


class RequiredChecksGuard:
    name = 'required-business-checks'

    def __init__(self, store, task_id, checks):
        self.store, self.task_id, self.checks = store, task_id, checks

    def check(self, action, scope, policy):
        if action.id.startswith(action.task + '/seed/'):
            return GateDecision(verdict='abstain')
        for check in self.checks:
            if action.tool not in check.before_tools:
                continue
            validator = Draft202012Validator(check.outcome_schema)
            if not any(row['action']['task'] == self.task_id and row['action']['tool'] == check.tool
                       and row.get('terminal') and row.get('outcome', {}).get('status') == 'completed'
                       and validator.is_valid(row['outcome']) for row in self.store.snapshot()['actions'].values()):
                return GateDecision(verdict='deny', reason='Run ' + check.tool + ' and resolve its result before this operation')
        return GateDecision(verdict='abstain')
