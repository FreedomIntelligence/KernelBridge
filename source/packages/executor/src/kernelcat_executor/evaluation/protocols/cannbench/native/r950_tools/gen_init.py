#!/usr/bin/env python3
'Generate cann_bench Python interfaces from op_plugin TORCH_LIBRARY schemas.\n\nThe exported interfaces must match the operators declared by the candidate.'
import re
import pathlib
import sys

# TorchScript 类型 -> Python 注解
_TY = {
    "Tensor": "torch.Tensor", "Tensor?": "Optional[torch.Tensor]",
    "int": "int", "int?": "Optional[int]", "int[]": "List[int]",
    "float": "float", "float?": "Optional[float]", "float[]": "List[float]",
    "bool": "bool", "bool?": "Optional[bool]", "bool[]": "List[bool]",
    "str": "str", "str?": "Optional[str]", "Scalar": "float", "SymInt": "int",
}


def _ann(t):
    t = t.strip()
    return _TY.get(t, "torch.Tensor" if t.startswith("Tensor") else "object")


def parse_schemas(plugin_dir: pathlib.Path):
    """Read schemas from plugin translation units and their local includes."""
    out = []
    root = plugin_dir.resolve()
    pending = sorted(root.glob("*.cpp"), reverse=True)
    seen = set()
    while pending:
        cpp = pending.pop().resolve()
        if cpp in seen:
            continue
        seen.add(cpp)
        src = cpp.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r'^\s*#\s*include\s*"([^"\n]+)"', src, re.MULTILINE):
            included = (cpp.parent / name).resolve()
            local_fragment = included.suffix == ".inc" or (
                included.suffix in {".h", ".hpp"} and included.is_file()
            )
            if included.is_relative_to(root) and local_fragment:
                pending.append(included)
        # m.def("name(args) -> ret");  —— 允许 C++ 里的 \" 转义
        # 🔴 schema 常常被写成**多个相邻字符串字面量**跨行拼接（C++ 自动拼接），
        #    例如 lstm / gru / mla / quant_matmul 等 13/55 个算子。原来的单字面量
        #    正则对它们一个都匹配不上 ⇒ render() 抛「没解析出任何算子 schema」
        #    ⇒ push910 在打包阶段就拒发，这些算子**根本推不上去**。
        #    下面按「一个或多个相邻字面量」抓取再拼接；单字面量是它的特例，
        #    所以对原来能解析的 42 个算子输出逐字不变（已逐个 diff 验证）。
        for m in re.finditer(r'm\.def\(\s*((?:"(?:[^"\\]|\\.)*"\s*)+)\)', src):
            lit = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))
            out.append(lit.replace('\\"', '"'))
    return out


def _split_args(s):
    """按顶层逗号切分参数（不能被 [] 或 "" 里的逗号骗到）。"""
    parts, depth, cur, q = [], 0, "", False
    for ch in s:
        if ch == '"':
            q = not q
        if not q:
            if ch in "[(":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(cur); cur = ""; continue
        cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def wrapper_for(schema: str):
    """schema -> (算子名, 生成的 def 源码)。解析不了就返回 None。"""
    m = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*->\s*(.+?)\s*$', schema, re.S)
    if not m:
        return None
    name, argstr, ret = m.group(1), m.group(2), m.group(3).strip()

    sig, call = [], []
    for a in _split_args(argstr):
        if a == "*":                      # keyword-only 分隔符
            sig.append("*"); continue
        default = None
        if "=" in a:
            a, default = a.split("=", 1)
            a, default = a.strip(), default.strip()
        toks = a.split()
        ty, nm = (" ".join(toks[:-1]), toks[-1]) if len(toks) >= 2 else (toks[0], f"arg{len(sig)}")
        py = f"{nm}: {_ann(ty)}"
        if default is not None:
            d = {"True": "True", "False": "False", "None": "None"}.get(default, default)
            py += f" = {d}"
        sig.append(py)
        call.append(nm)

    # 返回类型：可能是 "Tensor"、"Tensor indices"、"(Tensor a, Tensor b)"
    if ret.startswith("("):
        inner = _split_args(ret.strip("()"))
        rann = f"Tuple[{', '.join(_ann(x.split()[0]) for x in inner)}]"
    else:
        rann = _ann(ret.split()[0])

    body = (f"def {name}({', '.join(sig)}) -> {rann}:\n"
            f"    return torch.ops.cann_bench.{name}({', '.join(call)})\n")
    return name, body


HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CANN Bench - Ascend operator benchmarks

⚠ 本文件由 r950_tools/gen_init.py 从 op_plugin 的 TORCH_LIBRARY schema **自动生成**，
  不要手工编辑。计分 runner 会扫描这里导出的函数名去匹配 benchmark 的 task；
  匹配不上会**回落成评测全部 53 个算子**，进而触发 archive 阶段的
  "evaluation result contains unselected operator" 而整发被拒。
"""
__version__ = "1.0.0"
from typing import List, Optional, Tuple

import torch

try:
    from . import _C
except ImportError as e:
    raise ImportError(
        "Cannot import _C. Please make sure the `cann_bench` package is properly installed. "
    ) from e

'''


def render(pkg_root: pathlib.Path) -> tuple[str, list[str]]:
    """扫 pkg_root/csrc/ops/*/op_plugin/，生成 __init__.py 全文 + 导出的算子名。"""
    bodies, names = [], []
    for opdir in sorted((pkg_root / "csrc" / "ops").iterdir()):
        pdir = opdir / "op_plugin"
        if not pdir.is_dir():
            continue
        for sch in parse_schemas(pdir):
            r = wrapper_for(sch)
            if r is None:
                print(f"  ⚠ 解析不了 schema，跳过: {sch[:90]}", file=sys.stderr)
                continue
            nm, body = r
            if nm in names:
                continue
            names.append(nm); bodies.append(body)
    if not names:
        raise RuntimeError(f"{pkg_root} 下没解析出任何算子 schema —— 拒绝生成空接口")
    txt = HEADER + "\n".join(bodies) + "\n__all__ = " + repr(names) + "\n"
    return txt, names


def sync(pkg_root) -> list[str]:
    """就地重写 pkg_root/cann_bench/__init__.py，返回导出的算子名。"""
    pkg_root = pathlib.Path(pkg_root)
    txt, names = render(pkg_root)
    (pkg_root / "cann_bench").mkdir(exist_ok=True)
    (pkg_root / "cann_bench" / "__init__.py").write_text(txt, encoding="utf-8")
    return names


if __name__ == "__main__":
    for root in sys.argv[1:]:
        print(f"== {root}")
        for n in sync(root):
            print(f"   导出 {n}")
