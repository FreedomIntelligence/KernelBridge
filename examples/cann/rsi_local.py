"""Pinned local-NPU transport for the existing paired evaluator."""
import json
from pathlib import Path
import runpy
import sys

config = json.loads(Path(sys.argv[1]).read_text())
bundle = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(bundle / 'validation'))
name = 'cannbench_sigmoid_adapter.py' if config['kind'] == 'sigmoid' else 'native_rsi_adapter.py'
sys.argv = [str(bundle / 'validation' / name), '--state', config['state'], '--soc', config['soc'], '--device', str(config['device'])]
if config['kind'] == 'sigmoid':
    sys.argv += ['--suite', config['suite']]
else:
    sys.argv += ['--reference', config['reference'], '--operator', config['operator'], '--atol', str(config['atol']), '--rtol', str(config['rtol'])]
runpy.run_path(str(bundle / 'validation' / name), run_name='__main__')
