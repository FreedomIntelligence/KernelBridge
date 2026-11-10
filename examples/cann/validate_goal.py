"""Finite independent validation of original retained owner evidence."""
import ast
import hashlib
import json
import sys
from pathlib import Path
from kernelcat_executor.state.runtime import RuntimeJournal

config = json.loads(Path(sys.argv[1]).read_text())
output = Path(sys.argv[2])
manifest = json.loads((output / 'manifest.json').read_text())
journal = RuntimeJournal(Path(config['state_dir']) / 'events.jsonl')
snapshot = journal.snapshot()
actions = [row for row in snapshot['actions'].values()
           if row.get('binding', {}).get('run_id') == manifest['run_id']]
completed = [row for row in actions if row.get('outcome', {}).get('status') == 'completed']
audits = [row for row in completed if row['action']['tool'] == 'delegate'
          and row['action']['arguments'].get('agent') == 'kernel-reviewer'
          and row['action']['arguments'].get('mode') == 'read_only']
measured = []
for row in completed:
    if row['action']['tool'] not in ('__rsi_record__', '__rsi_apply__'):
        continue
    reference = row['outcome']['data'].get('private_reference')
    if reference is None:
        continue
    retained = json.loads(Path(reference['locator']).read_text())
    if retained['kind'] == 'node' and retained['value'].get('correct') is True:
        measured.append(retained['value'])
if config.get('rsi'):
    assert measured, 'No original correctness-passing measured RSI candidate'
trial_id = snapshot['runtime_runs'][manifest['run_id']]['trial']['trial_id']
state = journal.state(manifest['run_id'], trial_id)
reference = state['materialization']['result']['manifest_ref']
source = Path(reference['locator']).read_bytes()
assert len(source) == reference['byte_length'], 'Materialized candidate byte length changed'
assert 'sha256:' + hashlib.sha256(source).hexdigest() == reference['digest'], 'Materialized candidate digest changed'
files = [f for f in json.loads(source)['files'] if f['path'] == 'solution.py']
assert len(files) == 1, 'Expected one native source candidate'
tree = ast.parse(files[0]['text'])
names = {target.id for statement in tree.body if isinstance(statement, ast.Assign)
         for target in statement.targets if isinstance(target, ast.Name)}
assert {'project_json_src', 'host_tiling_src', 'host_operator_src', 'kernel_src',
        'python_bind_src', 'model_src'} <= names, 'Retained program is not a native six-source candidate'
def source_digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
actual_digest = source_digest(files)
# Correctness must bind these exact native bytes, never a Torch-only fallback.
if config.get('rsi'):
    assert any(node['source_digest'] == actual_digest and node['retained_source_digest'] == actual_digest
               for node in measured), 'Materialized candidate has no matching correct original RSI measurement'
else:
    from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
    from kernelcat_executor.state.runtime_store import RuntimeStore
    from kernelcat_executor.state.runtime_state import actor_from
    from kernelcat_worker.configuration import Config
    from kernelcat_worker.candidate_retention import qualified_source
    cfg = Config.model_validate_json(json.dumps(config))
    current = snapshot['runtime_runs'][manifest['run_id']]
    retained = qualified_source(RuntimeStore(journal, actor_from(state['stamp'])),
        RuntimeArtifacts(Path(config['state_dir']) / 'runtime-artifacts'), current['task']['id'], cfg.candidate_retention)
    assert retained == files, 'Materialized source lacks a passing, source-bound functional receipt'

assert not state['pending_effect_ids'], 'Original effects remain unresolved'
print(json.dumps({'ok': True, 'audit_receipts': len(audits), 'correct_measured_nodes': len(measured)}))
