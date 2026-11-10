"""Offline PyPTO adapters: retain raw evidence, join by core/PC, never infer scores."""
import collections
import copy
import csv
import hashlib
import html
import json
from pathlib import Path
import struct

VENDOR = Path(__file__).parent / 'vendor/pypto'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def fingerprint(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def verify_vendor():
    manifest = read(VENDOR / 'provenance.json')
    for item in manifest['files']:
        if fingerprint(VENDOR / item['file'])['sha256'] != item['sha256']:
            raise ValueError('vendored source changed: ' + item['file'])
    return manifest['revision']


def pc_key(value):
    if value is None:
        return None
    return hex(int(value, 16) if isinstance(value, str) else int(value))


def blocks(data):
    """Validate complete framing before the upstream parser strips padding/path."""
    from vendor.pypto import clean_sim_trace as upstream
    offset = 0
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError('truncated block header')
        size = struct.unpack_from('<Q', data, offset)[0]
        if size > len(data) - offset - 12:
            raise ValueError('truncated block payload')
        offset += 12 + size
    result = list(upstream.iter_blocks(data))
    counts = collections.Counter(k for k, _ in result)
    if counts[2] != 1 or counts[4] > 1:
        raise ValueError('expected exactly one TRACE and at most one API_INSTR block')
    return result


def bind_identity(path, archive=None, binary=None, trace_hash=None):
    identity = read(path) if path else {}
    claimed = identity.get('trace_sha256')
    if claimed and claimed != trace_hash:
        raise ValueError('capture identity trace hash mismatch')
    result = dict(record=fingerprint(path) if path else None, claims=identity,
                  capture_link='recorded_by_collector' if path else 'unavailable', verified_files={})
    for name, artifact, expected in [
        ('source_archive', archive, identity.get('source_archive_sha256') or identity.get('source_sha256')),
        ('device_binary', binary, identity.get('binary_sha256'))]:
        if artifact:
            actual = fingerprint(artifact)
            if not expected or expected != actual['sha256']:
                raise ValueError(name + ' hash missing or mismatched')
            result['verified_files'][name] = actual
    return result


def signature(event):
    return (event.get('pid'), event['name'], event['ts'], event.get('dur'),
            pc_key(event.get('args', {}).get('pc_addr')))


def audit_fidelity(raw, clean, keep_scalar):
    from vendor.pypto import clean_sim_trace as upstream
    expected = [e for e in raw['traceEvents'] if e.get('ph') == 'X'
                and e.get('tid') not in upstream._DROP_LANES and e['name'] not in upstream._SYNC_NAMES
                and (keep_scalar or e.get('tid') != 'SCALAR')]
    actual = [e for e in clean['traceEvents'] if e.get('ph') == 'X']
    if collections.Counter(map(signature, expected)) != collections.Counter(map(signature, actual)):
        raise ValueError('cleaner altered retained instruction identity/timing')
    lanes = collections.defaultdict(list)
    for e in actual:
        lanes[e['pid'], e['tid']].append(e)
    for events in lanes.values():
        events.sort(key=lambda e: e['ts'])
        if any(a['ts'] + a.get('dur', 0) > b['ts'] + 1e-10 for a, b in zip(events, events[1:])):
            raise ValueError('overlap remains inside a split display lane')
    return dict(retained_events=len(actual), identities_and_times_unchanged=True, split_lanes_nonoverlapping=True)


def join_metrics(clean, metrics):
    events = collections.defaultdict(list)
    for e in clean['traceEvents']:
        if e.get('ph') == 'X':
            events[e['pid'], pc_key(e.get('args', {}).get('pc_addr'))].append(e)
    rows = []
    for core, instructions in metrics.get('instructions', {}).items():
        for r in instructions:
            if r.get('instructions_executed', 0) <= 0:
                continue
            pc = pc_key(r.get('address'))
            matched = events.get((core, pc), []) if pc is not None else []
            mnemonic = str(r.get('source', '')).split(' ', 1)[0]
            row = dict(core=core, pc=pc, instruction=mnemonic, pipe=r.get('pipe'),
                executions=r['instructions_executed'], trace_events=len(matched), cycles=r.get('cycles'),
                first_us=min((e['ts'] for e in matched), default=None),
                last_us=max((e['ts'] + e.get('dur', 0) for e in matched), default=None),
                first_operands=matched[0].get('args', {}).get('detail') if matched else None,
                source_annotation=r.get('ascendc_inner_code') or None,
                diagnostic='parser_semantics_suspect' if mnemonic == 'VAXPY' else 'model_not_hardware_pmu')
            for key, target in [('ub_read_conflict', 'ub_read_conflict_model'),
                                ('ub_write_conflict', 'ub_write_conflict_model'),
                                ('vector_utilization_percentage', 'vector_payload_percentage')]:
                value = r.get(key)
                row[target] = value if isinstance(value, (int, float)) and value >= 0 else None
            rows.append(row)
    return rows


CAVEATS = [
    '模拟 cycle / 时间不是板上耗时或官方分数；指令区间可能包含排队，不能相加成关键路径。',
    '清洗图隐藏 WAIT/SET/BAR，必须同时看 trace.raw.json；箭头仅重建已匹配的 flag，不是完整依赖图。',
    '拆分泳道是显示层处理，不代表额外硬件 pipe；保留的指令时间、PC 和操作数不改变。',
    'UB conflict 是解析器诊断模型；VAXPY 的 XM 操作数解释存在疑点，不能据此直接调整 padding。',
    'vector_payload_percentage 是相对最大 repeat 载荷的比例，不是管道忙碌率；负值保留为不可用。',
    'API_INSTR.Source 是指令文本；AscendC Inner Code 非空也需核对调试构建和源码身份。',
    'SOURCE 块存在与否不等于 PC 映射成立；本工具不伪造 PC → C++ → IR/Pass 的关联。',
]


def coverage_from_hardware(path, identity, observed):
    if not path:
        return dict(observed_aiv=observed, expected_aiv=None, complete=None)
    hardware = read(path)
    if not identity.get('binary_sha256') or hardware.get('binary_sha256') != identity['binary_sha256']:
        raise ValueError('coverage hardware report binary mismatch')
    if identity.get('tokens_override'):
        raise ValueError('prefix capture cannot use original hardware launch coverage')
    launches = [s for s in hardware['samples'] if s['case'] == identity.get('case')]
    counts = {int(s['stages'][0]['Mix Block Num']) for s in launches if len(s['stages']) == 1}
    if len(counts) != 1 or any(len(s['stages']) != 1 for s in launches):
        raise ValueError('coverage needs one consistent single-kernel launch')
    expected = counts.pop()
    return dict(observed_aiv=observed, expected_aiv=expected, complete=observed == expected,
                reference=fingerprint(path), basis='matching binary and case in separate hardware capture')


def clean_capture(path, output, identity=None, archive=None, binary=None, keep_scalar=True, hardware=None):
    revision = verify_vendor()
    from vendor.pypto import clean_sim_trace as upstream
    source = upstream._resolve_input(Path(path)).resolve()
    data = source.read_bytes()
    capture = dict(path=str(source), sha256=hashlib.sha256(data).hexdigest())
    binding = bind_identity(identity, archive, binary, capture['sha256'])
    parsed = blocks(data)
    raw = json.loads(next(v for k, v in parsed if k == 2))
    clean, skipped = upstream.rebuild_trace(copy.deepcopy(raw), keep_scalar=keep_scalar)
    fidelity = audit_fidelity(raw, clean, keep_scalar)
    api = next((json.loads(v) for k, v in parsed if k == 4), None)
    metrics = upstream.reshape_metrics(api) if api is not None else {'instructions': {}, 'cores': []}
    joined = join_metrics(clean, metrics)
    per_core = {}
    for core in sorted({e['pid'] for e in raw['traceEvents'] if e.get('ph') == 'X'}):
        events = [e for e in raw['traceEvents'] if e.get('pid') == core and e.get('ph') == 'X'
                  and e.get('tid') not in upstream._DROP_LANES and e['name'] not in upstream._SYNC_NAMES]
        if not events:
            continue
        start = min(e['ts'] for e in events)
        finish = max(e['ts'] + e.get('dur', 0) for e in events)
        per_core[core] = dict(first_us=start, last_us=finish, instruction_envelope_us=finish-start,
            instruction_events=len(events), pipe_counts=dict(collections.Counter(e['tid'] for e in events)),
            opcode_counts=dict(collections.Counter(e['name'] for e in events)))
    report = dict(schema_version=1, kind='simulator', capture=capture, identity=binding,
        upstream_revision=revision, keep_scalar=keep_scalar, fidelity=fidelity,
        blocks=dict(collections.Counter(k for k, _ in parsed)), skipped_sync_arrows=skipped,
        matched_flows=sum(e.get('ph') == 's' for e in clean['traceEvents']),
        active_core_pc_records=len(joined), joined_records=sum(r['trace_events'] > 0 for r in joined),
        capabilities=dict(instruction_metrics=api is not None, source_blocks=any(k == 1 for k, _ in parsed),
            source_annotations=sum(bool(r['source_annotation']) for r in joined),
            pc_to_cpp='unavailable', pc_to_ir_pass='unavailable'),
        cores=per_core, caveats=CAVEATS, performance_claim=None)
    report['coverage'] = coverage_from_hardware(hardware, binding['claims'],
                                               sum('veccore' in str(k) for k in per_core))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'trace.raw.json', raw)
    save(output / 'trace.clean.json', clean)
    save(output / 'instr_metrics.json', metrics)
    save(output / 'pc_metrics.json', joined)
    if joined:
        with (output / 'pc_metrics.csv').open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=list(joined[0]))
            writer.writeheader(); writer.writerows(joined)
    if identity:
        (output / 'capture_identity.json').write_bytes(Path(identity).read_bytes())
    report['outputs'] = {p.name: fingerprint(p)['sha256'] for p in output.iterdir() if p.is_file()}
    save(output / 'evidence.json', report)
    summary = ''.join(f'<li>{html.escape(c)}</li>' for c in CAVEATS)
    links = ''.join(f'<li><a href="{n}">{n}</a></li>' for n in report['outputs'])
    (output / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Trace evidence</title>'
        '<style>body{font:16px system-ui;max-width:1000px;margin:40px auto;line-height:1.7}</style>'
        '<h1>Trace 证据包</h1><p>按同一 capture 的 core＋PC 关联；时间单位 µs。</p>'
        f'<p>采集 SHA256：{capture["sha256"]}</p><ul>{links}</ul><ul>{summary}</ul>'
        '<p>PC→C++→IR/Pass 尚无自动映射。evidence.json 记录实际能力和缺口。</p>', encoding='utf-8')
    return dict(output=str(output.resolve()), capture_sha256=capture['sha256'], fidelity=fidelity,
                active_core_pc_records=len(joined), capabilities=report['capabilities'], coverage=report['coverage'])


def inspect_pc(directory, core, pc):
    directory = Path(directory)
    evidence = read(directory / 'evidence.json')
    for name in ('pc_metrics.json', 'trace.raw.json'):
        if fingerprint(directory / name)['sha256'] != evidence['outputs'][name]:
            raise ValueError('evidence output changed: ' + name)
    key = pc_key(pc)
    return dict(capture=evidence['capture'], identity=evidence['identity'], core=core, pc=key,
        metrics=[r for r in read(directory / 'pc_metrics.json') if r['core'] == core and r['pc'] == key],
        raw_events=[e for e in read(directory / 'trace.raw.json')['traceEvents']
                    if e.get('pid') == core and pc_key(e.get('args', {}).get('pc_addr')) == key],
        capabilities=evidence['capabilities'])


def add_cli(sub):
    trace = sub.add_parser('trace', help='Offline PyPTO trace and IR evidence tools')
    commands = trace.add_subparsers(dest='trace_command', required=True)
    p = commands.add_parser('clean')
    p.add_argument('input', type=Path); p.add_argument('-o', '--output', type=Path, required=True)
    p.add_argument('--identity', type=Path); p.add_argument('--source-archive', type=Path)
    p.add_argument('--device-binary', type=Path); p.add_argument('--drop-scalar', action='store_true')
    p.add_argument('--hardware-summary', type=Path, help='Check observed AIV count against matching profile_selected summary')
    p = commands.add_parser('pc')
    p.add_argument('directory', type=Path); p.add_argument('--core', required=True); p.add_argument('--pc', required=True)
    p = commands.add_parser('ir')
    p.add_argument('inputs', type=Path, nargs='+'); p.add_argument('--format', choices=['llvm', 'pypto'], required=True)
    p.add_argument('-o', '--output', type=Path, required=True)
    p.add_argument('--function', help='LLVM definition-name regex; displayed line numbers are excerpt-relative')


def run(args):
    if args.trace_command == 'clean':
        return clean_capture(args.input, args.output, args.identity, args.source_archive,
                             args.device_binary, not args.drop_scalar, args.hardware_summary)
    if args.trace_command == 'pc':
        return inspect_pc(args.directory, args.core, args.pc)
    from ir_bridge import render_ir
    return render_ir(args.inputs, args.output, args.format, args.function)
