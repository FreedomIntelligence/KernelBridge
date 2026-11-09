"""批量跑 selected_ops.yaml 里全部算子的 schema 提取,定位边界 case。

每个算子起独立 subprocess,避免内存累积/OOM (有些 reference 的 input 单个就 1GB)。

用法:
    python batch_validate_schema.py [<selected_ops.yaml>] [--reference-root /absolute/reference]

源码检出使用协议资源根；独立安装时显式提供 selection 和 reference root。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

THIS_DIR = Path(__file__).resolve().parent
MKB_ROOT = THIS_DIR.parent
RESOURCE_ROOT = THIS_DIR.parents[2] / "evaluation/protocols/cannbench"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection", nargs="?", type=Path, default=RESOURCE_ROOT / "selected_ops.yaml")
    parser.add_argument("--reference-root", type=Path, default=RESOURCE_ROOT / "reference")
    args = parser.parse_args()
    yaml_path = args.selection.resolve(strict=True)
    base_dir = args.reference_root.resolve(strict=True)
    if not base_dir.is_dir():
        parser.error("reference root must be an existing directory")
    with yaml_path.open() as stream:
        data = yaml.safe_load(stream)

    ops = data["ops"]
    print(f"validating {len(ops)} ops from {yaml_path}")
    print(f"base_dir = {base_dir}")
    print(f"runner   = {sys.executable} (subprocess per op)\n")

    ok, fail = [], []
    for i, op in enumerate(ops, 1):
        name = op["name"]
        category = op["category"]
        rel = op["path"]
        full = base_dir / rel

        # 子进程跑,JSON 输出
        cmd = [sys.executable, "-m", "kernelcat_executor.evaluation.protocols.six_source.native.schema",
               str(full), name, category, "--json"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=180, cwd=str(MKB_ROOT))
        except subprocess.TimeoutExpired:
            print(f"FAIL  [{category:>13}] {name:55s} TIMEOUT(>180s)")
            fail.append((name, category, "timeout", ""))
            continue

        if p.returncode != 0:
            err = p.stderr.strip().splitlines()[-1] if p.stderr.strip() else f"exit={p.returncode}"
            print(f"FAIL  [{category:>13}] {name:55s} {err}")
            fail.append((name, category, err, p.stderr))
            continue

        try:
            schema = json.loads(p.stdout)
        except json.JSONDecodeError as e:
            print(f"FAIL  [{category:>13}] {name:55s} JSON parse: {e}")
            fail.append((name, category, str(e), p.stdout[-500:]))
            continue

        n_in  = len(schema.get("inputs", []))
        n_p   = len(schema.get("parameters", []))
        n_b   = len(schema.get("buffers", []))
        n_a   = len(schema.get("init_attrs", []))
        n_out = len(schema.get("outputs", []))
        ok.append((name, schema))
        print(f"  OK  [{category:>13}] {name:55s} "
              f"in={n_in} param={n_p} buf={n_b} attr={n_a} out={n_out}")

    print(f"\n=== summary: {len(ok)}/{len(ops)} OK, {len(fail)} FAIL ===")
    if fail:
        print("\n--- failures ---")
        for name, cat, err, detail in fail:
            print(f"\n### {cat}/{name}")
            print(f"error: {err}")
            if detail:
                # 只打末尾几行 stderr
                tail = "\n".join(detail.strip().splitlines()[-15:])
                print(tail)
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
