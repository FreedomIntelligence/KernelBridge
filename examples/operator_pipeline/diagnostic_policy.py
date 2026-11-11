"""Shared finite diagnostic policy for workflow drivers and in-process NPU jobs.

This module never launches work. It selects collection and validates the actual
source-bound artifacts produced by an already authorized evaluation process.
"""
import hashlib
from pathlib import Path


def diagnostic_reason(policy, *, failed, regression):
    if not policy:
        return None
    if policy.get('required', False):
        return 'required'
    if failed and policy.get('on_failure', True):
        return 'failure'
    if regression and policy.get('on_regression', True):
        return 'regression'
    return None


def verify_record(folder, data, source_sha256, scope, kind):
    folder = Path(folder).resolve()
    if (data.get('kind') != kind or data.get('scope') != scope
            or data.get('candidate_sha256') != source_sha256):
        raise ValueError('Diagnostic kind, scope or source identity differs from the request')
    artifacts = data.get('artifacts')
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError('Diagnostic needs original artifacts, not an empty success marker')
    for name, expected in artifacts.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise ValueError('Diagnostic artifact names and hashes must be strings')
        path = folder / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or path.resolve() != path
                or not path.is_relative_to(folder) or not path.is_file() or path.stat().st_size == 0
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError('Diagnostic artifact missing, changed or outside the original run')


def assess_diagnostics(policy, evaluation, records, artifact_root, source_sha256, *, binding=None, handle=None):
    """Assess in-process diagnostics without recompiling or relaunching a job.

    Persist this return value in the original evaluation result. `satisfied`
    describes diagnostic completeness only; it cannot certify kernel acceptance.
    """
    failed = evaluation.get('status') != 'completed' or evaluation.get('correct') is not True
    speedup = evaluation.get('geomean_speedup', evaluation.get('speedup'))
    regression = speedup is not None and speedup <= 1 or any(
        row.get('speedup', 1) < 1 for row in evaluation.get('cases', []) if isinstance(row, dict))
    reason = diagnostic_reason(policy, failed=failed, regression=regression)
    if reason is None:
        return {'triggered': False, 'reason': None, 'required': False, 'satisfied': None, 'reports': []}
    scope = policy.get('scope')
    if not isinstance(scope, str) or not scope:
        raise ValueError('Diagnostic policy needs an explicit evidence scope')
    reports = []
    for kind in ('ir', 'profiler'):
        found = [record for record in records if record.get('kind') == kind]
        report = dict(found[0]) if len(found) == 1 else {'kind': kind, 'status': 'unavailable', 'reason': 'No diagnostic artifact record'}
        if len(found) > 1:
            report = {'kind': kind, 'status': 'failed', 'reason': 'Ambiguous duplicate diagnostic records'}
        if report.get('status', 'completed') not in {'completed', 'failed', 'unavailable', 'unknown', 'blocked'}:
            report.update(status='failed', reason='Invalid diagnostic completion status')
        if found and len(found) == 1 and report.get('status', 'completed') == 'completed':
            try:
                verify_record(artifact_root, report, source_sha256, scope, kind)
                report['status'] = 'completed'
            except (OSError, ValueError) as error:
                report.update(status='failed', reason=str(error))
        reports.append(report)
    return {'triggered': True, 'reason': reason, 'required': bool(policy.get('required', False)),
            'satisfied': all(row['status'] == 'completed' for row in reports), 'reports': reports,
            'source_sha256': source_sha256, 'binding': binding, 'original_handle': handle,
            'next_step': 'Read original IR/profiler artifacts and revise the hypothesis; diagnostic completeness is not correctness or performance acceptance.'}
