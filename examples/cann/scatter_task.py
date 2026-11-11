"""Source-only scatter task authoring; never labels an untested baseline verified."""
import json


def build(case, baseline, objective, mode):
    from make_tasks import FIELDS
    reduce = case['kind'].removeprefix('scatter_')
    if reduce not in ('max', 'min') or mode not in ('generate', 'optimize'):
        raise ValueError('Unsupported scatter task')
    n, features = case['shape']; bins = case['dim_size']
    if any(type(v) is not int or v < 0 for v in (n, features, bins)) or n and not bins:
        raise ValueError('Invalid scatter shape')
    if case['distribution'] != 'standard_normal': raise ValueError('Unsupported scatter distribution')
    reference = f'''import torch
SEEDS = {case['seeds']!r}
_trial = 0
class Model(torch.nn.Module):
    def forward(self, src, index):
        # Independent grouped reduction: no torch_scatter dependency or candidate import.
        x = src.detach().cpu(); ids = index.detach().cpu()
        out = torch.zeros(({bins}, {features}), dtype=src.dtype)
        for destination in range({bins}):
            group = x[ids == destination]
            if group.shape[0]: out[destination] = group.{('amax' if reduce == 'max' else 'amin')}(dim=0)
        return out.to(src.device)
def get_inputs():
    global _trial
    g = torch.Generator().manual_seed(SEEDS[_trial % len(SEEDS)]); _trial += 1
    return [torch.randn(({n}, {features}), generator=g, dtype=torch.float32),
            torch.randint({max(bins, 1)}, ({n},), generator=g, dtype=torch.int64)]
def get_init_inputs(): return []
'''
    contract = {'operator': case['kind'], 'native_operator': case['native_operator'],
        'target': {'soc': case['soc'], 'device': case['device']},
        'inputs': [{'name': 'src', 'shape': [n, features], 'dtype': 'float32', 'layout': 'contiguous ND'},
                   {'name': 'index', 'shape': [n], 'dtype': 'int64', 'layout': 'contiguous ND'}],
        'output': {'shape': [bins, features], 'dtype': 'float32'},
        'semantics': f'Feature-index scatter {reduce} along dim=0; values only; empty bins zero; valid indices; finite FP32 inputs strictly inside extrema.',
        'correctness': {'atol': case['atol'], 'rtol': case['rtol'], 'finite_inputs': True},
        'test_data': {'seeds': case['seeds'], 'correctness_trials': 5},
        'candidate': {'path': 'solution.py', 'format': 'six_source', 'required_fields': FIELDS},
        'baseline_evidence': 'unverified source; run baseline_check before any optimization',
        'input_mutation_allowed': False}
    goal = objective + '\n' + contract['semantics'] + '\nRead the frozen contract and reference. Implement solution.py as six native Ascend C source fields. '
    goal += 'The supplied baseline has no device acceptance yet. Compile and run all five correctness seeds before using it as an incumbent. '
    goal += 'Use the configured evaluator and diagnostic tools, retain original receipts, and claim speedup only after full same-device paired measurement.'
    task = {'id': case['task_id'], 'scope': 'cann-operator-generation', 'goal': goal, 'contract': contract,
        'seed_files': {'solution.py': baseline if mode == 'optimize' else '"""Implement the frozen scatter contract."""\n',
                      'reference/case.json': json.dumps(case, indent=2) + '\n', 'reference/reference.py': reference,
                      'reference/native-template.py': baseline}}
    request = {'candidate': './workspace/solution.py', 'reference': './reference.py', 'workspace': './new-evaluation-job',
        'operator': case['native_operator'], 'soc': case['soc'], 'device': case['device'], 'fast': False,
        'timeout': 600, 'atol': case['atol'], 'rtol': case['rtol']}
    return task, reference, request
