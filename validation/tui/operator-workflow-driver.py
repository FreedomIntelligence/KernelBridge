"""Deterministic protocol driver; these numbers/artifacts are NOT device evidence."""
import hashlib
import json
from pathlib import Path
import sys

request = json.load(sys.stdin)
kind = sys.argv[1]
source = Path(request['source']['root']) / 'candidate.py'
value = int(source.read_text().split('=')[1])
ids = [case['id'] for case in request['contract']['suite']]
if kind == 'launch':
    result = {'handle': {'job': request['source']['sha256'], 'pid': 1, 'log': 'protocol-fixture.log'}}
elif kind == 'collect':
    result = {'handle': request['handle'], 'phase': 'complete'}
elif kind == 'check':
    result = {'checks': dict.fromkeys(['build', 'semantics', 'full'], value != 0),
              'case_count': len(ids), 'case_ids': ids, 'oracle_sha256': request['contract']['oracle']['files']['sha256'],
              'scope': 'protocol_fixture'}
elif kind == 'measure':
    latency = 100 if request['role'] == 'A' else (110 if value == 1 else 90)
    result = {'correct': True, 'environment': {'scope': 'protocol_fixture'},
              'measurement': request['contract']['measurement'], 'cases': {case: [latency] * 3 for case in ids}}
elif kind == 'calibrate':
    result = {'scope': 'protocol_fixture', 'official_submission': False, 'receipt': 'local-calibration-fixture'}
else:
    path = Path(request['output']) / (kind + '.txt')
    path.write_text('PROTOCOL FIXTURE, not real IR or profiling. source=' + request['source']['sha256'])
    result = {'kind': kind, 'scope': 'protocol_fixture', 'candidate_sha256': request['source']['sha256'],
              'artifacts': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}}
print(json.dumps(result))
