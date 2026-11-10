"""Reuse PyPTO's renderer, with an explicit text-only adapter for BiSheng LLVM."""
import difflib
from collections import Counter
import html
from pathlib import Path
import re

from trace_lab import fingerprint, save, verify_vendor


def llvm_stats(text):
    return dict(definitions=len(re.findall(r'^define ', text, re.M)),
        debug_locations=len(re.findall(r'!DILocation\(', text)),
        calls=dict(Counter(re.findall(r'\bcall\b[^\n]*?@([^ (]+)\(', text))))


def select_llvm(text, pattern):
    if not pattern:
        return text
    definitions = list(re.finditer(r'^define [^\n]*@([^ (]+)[^\n]*\{\n.*?^\}', text, re.M | re.S))
    selected = [m for m in definitions if re.search(pattern, m[1])]
    if not selected:
        raise ValueError('no LLVM definition matches: ' + pattern)
    return '\n\n'.join('; LLVM source line ' + str(text.count('\n', 0, m.start())+1) + '\n' + m[0]
                       for m in selected)


def render_ir(inputs, output, format, function=None):
    revision = verify_vendor()
    from vendor.pypto.ir_trace.html import render_html
    from vendor.pypto.ir_trace.model import Snapshot, PassTrace, DiffSection, DiffHunk, DiffRow, split_source_lines
    if format == 'pypto':
        if len(inputs) != 1 or function:
            raise ValueError('PyPTO requires one passes_dump directory')
        from vendor.pypto.ir_trace.discovery import discover_snapshots
        from vendor.pypto.ir_trace.diff import build_trace
        snapshots = discover_snapshots(inputs[0])
        traces = build_trace(snapshots, context=3)
    else:
        if len(inputs) < 2 or any(p.suffix != '.ll' for p in inputs):
            raise ValueError('LLVM requires at least two .ll snapshots in explicit comparison order')
        snapshots = []
        for i, p in enumerate(inputs):
            text = select_llvm(p.read_text(encoding='utf-8'), function)
            snapshots.append(Snapshot(i, p.parent.name + '/' + p.name, p, text, split_source_lines(text),
                'LLVM 文本对照：文件顺序不代表编译 Pass 顺序。无 PC 到源码映射。'
                + (' 行号相对于筛选摘录；首行标明原 LLVM 文件行号。' if function else '')))
        snapshots = tuple(snapshots)
        traces = []
        for i, (before, after) in enumerate(zip(snapshots, snapshots[1:]), 1):
            # Plain text correspondence only: do not use Python AST/token matching for LLVM.
            matcher = difflib.SequenceMatcher(None, before.lines, after.lines)
            rows, inserted, deleted = [], 0, 0
            for tag, a, b, c, d in matcher.get_opcodes():
                inserted += d-c if tag != 'equal' else 0
                deleted += b-a if tag != 'equal' else 0
                for j in range(max(b-a, d-c)):
                    left, right = a+j if a+j < b else None, c+j if c+j < d else None
                    rows.append(DiffRow(tag, left+1 if left is not None else None,
                        html.escape(before.lines[left]) if left is not None else '',
                        right+1 if right is not None else None,
                        html.escape(after.lines[right]) if right is not None else ''))
            # Reuse only the vendor's presentation folding, independent of IR syntax.
            from vendor.pypto.ir_trace.diff import _fold_rows
            section = DiffSection(None, None, inserted, deleted, _fold_rows(tuple(rows), 3))
            traces.append(PassTrace(i, after.pass_name, before, after, inserted, deleted, (section,)))
        traces = tuple(traces)
    output = Path(output)
    if output.exists() or output.with_suffix('.json').exists():
        raise ValueError('output already exists; use a new report name')
    output.parent.mkdir(parents=True, exist_ok=True)
    title = ('LLVM 文本对照（无 PC 映射）'
             if format == 'llvm' else 'PyPTO Python IR pass dumps')
    output.write_text(render_html(traces, source_name=title), encoding='utf-8')
    evidence = dict(format=format, upstream_revision=revision, report=fingerprint(output),
        inputs=[dict(fingerprint(s.path), statistics=llvm_stats(s.text) if format == 'llvm' else None)
                for s in snapshots], pc_mapping='unavailable',
        sequence='explicit_input_order' if format == 'llvm' else 'numbered_pypto_pass_dumps',
        function_filter=function, displayed_lines='excerpt_relative' if function else 'physical_file_lines',
        limitation='Static snapshot/text evidence only; not dynamic instruction counts or performance.')
    save(output.with_suffix('.json'), evidence)
    return dict(output=str(output.resolve()), format=format, snapshots=len(snapshots), pc_mapping='unavailable')
