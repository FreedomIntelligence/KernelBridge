#!/usr/bin/env python3
"""2-op C3D wall pack: Conv3DBackpropFilter + Gelu.

Never 1-op official std (1400s golden wall). Never the timed-out 4-op
pack (Transpose+Cummin+ArgMax, 3800s staged_timeout).

  python3 push_c3d_2op.py /path/to/conv_3d_backprop_filter
  python3 push_c3d_2op.py /path/to/arm --pack-only
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("CANN_BOARD", "910c")

from push910 import capacity, pick, rec, wait_job, fire_hidden, snapshot, write_zip, to_lf_tree
import gen_init
import wave as W

LIVE = HERE / "livesrc910"
COMPANION = ("Gelu", LIVE / "gelu", "gelu")
SNAKE = {
    "Conv3DBackpropFilter": "conv_3d_backprop_filter",
    "Gelu": "gelu",
}


def build_pkg(c3d: Path) -> tuple[str, str, list[str]]:
    ops = [
        ("Conv3DBackpropFilter", c3d),
        (COMPANION[0], COMPANION[1]),
    ]
    for name, src in ops:
        if not src.is_dir():
            raise SystemExit(f"missing {name} {src}")
    tmpl = HERE.parent / "cann_submit" / "template"
    stage = tempfile.mkdtemp(prefix="c3d2op_")
    pkg = os.path.join(stage, "pkg")
    shutil.copytree(tmpl, pkg)
    ops_dir = Path(pkg) / "csrc" / "ops"
    for d in list(ops_dir.iterdir()):
        if d.is_dir():
            shutil.rmtree(d)
    for name, src in ops:
        shutil.copytree(
            src,
            ops_dir / SNAKE[name],
            symlinks=False,
            ignore=shutil.ignore_patterns(
                "._*", "__pycache__", "*.pyc", "*.pyo",
                "verify_host.py", "*.md",
            ),
        )
    exported = gen_init.sync(pkg)
    want = set(SNAKE.values())
    if not want.issubset(set(exported)):
        raise RuntimeError(f"cann_bench export {exported} missing {want - set(exported)}")
    to_lf_tree(Path(pkg))
    zp = os.path.join(stage, "s.zip")
    write_zip(Path(pkg), Path(zp))
    declared = [n for n, _ in ops]
    return stage, zp, declared


def pack_only(c3d: Path) -> int:
    import zipfile

    stage, zp, declared = build_pkg(c3d)
    try:
        with zipfile.ZipFile(zp) as zf:
            names = zf.namelist()
        print("PACK_OK declared", declared, "bytes", Path(zp).stat().st_size)
        print("PACK_OPS", sorted({n.split("/")[2] for n in names if n.startswith("csrc/ops/") and "/" in n[9:]}))
        bad_4op = any(
            x in "/".join(names)
            for x in ("/transpose/", "/cummin/", "/arg_max/")
        )
        arm_bad = any("/arm/" in n or n == "csrc/ops/arm" or n.startswith("csrc/ops/arm/") for n in names)
        print("PACK_NOT_4OP" if not bad_4op else "PACK_HAS_4OP")
        print("PACK_NAME_OK" if not arm_bad else "PACK_NAME_BAD")
        return 0 if not bad_4op and not arm_bad else 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--pack-only"]
    pack = "--pack-only" in sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    c3d = Path(args[0]).resolve()
    if not (c3d / "op_plugin").is_dir():
        print("need conv_3d_backprop_filter tree with op_plugin/", file=sys.stderr)
        return 2
    if pack:
        return pack_only(c3d)

    stage, zp, declared = build_pkg(c3d)
    print("cann_bench", declared, flush=True)
    cap = capacity()
    tv = pick(cap)
    if tv is None:
        print("no credit/slot", flush=True)
        rec(kind="SKIP", op="Conv3DBackpropFilter", note="c3d-2op-gelu", detail="no credit")
        shutil.rmtree(stage, ignore_errors=True)
        return 1
    http, data = W.submit_zip(zp, declared, os.environ[tv], tv)
    shutil.rmtree(stage, ignore_errors=True)
    d = data if isinstance(data, dict) else {}
    jid = (d.get("job") or {}).get("id")
    sub = (d.get("submission") or {}).get("id")
    if not jid:
        print("submit fail", http, d, flush=True)
        rec(kind="FAIL", op="Conv3DBackpropFilter", note="c3d-2op-gelu", http=http, acct=tv)
        return 1
    rec(
        kind="SUBMIT",
        op="Conv3DBackpropFilter",
        note="c3d-2op-gelu",
        job=jid,
        sub=sub,
        acct=tv,
        detail=f"{jid} ops={declared}",
    )
    print("submitted", jid, sub, tv, declared, flush=True)

    m = wait_job(jid, tv, timeout=4500)
    st = m.get("status")
    sub = m.get("submission_id") or sub
    rec(kind="LAND", op="Conv3DBackpropFilter", note="c3d-2op-gelu", detail=f"{jid} status={st}")
    print("LAND", st, flush=True)
    if st != "succeeded":
        rec(kind="HIDDEN_SKIP", op="Conv3DBackpropFilter", note="c3d-2op-gelu", detail=f"status={st}")
        tot, delta, moved = snapshot()
        print(f"board {tot:.2f} delta={delta:+.2f}", flush=True)
        return 0
    hj, err = fire_hidden(sub, tv)
    if hj:
        rec(kind="HIDDEN", op="Conv3DBackpropFilter", note="c3d-2op-gelu", detail=f"{hj} from={sub}")
        print("hidden", hj, flush=True)
        hm = wait_job(hj, tv, timeout=4500)
        rec(kind="LAND_HIDDEN", op="Conv3DBackpropFilter", note="c3d-2op-gelu",
            detail=f"{hj} status={hm.get('status')}")
        print("LAND_HIDDEN", hm.get("status"), flush=True)
    else:
        rec(kind="HIDDEN_FAIL", op="Conv3DBackpropFilter", note="c3d-2op-gelu", detail=str(err))
        print("hidden fail", err, flush=True)
    tot, delta, moved = snapshot()
    print(f"board {tot:.2f} delta={delta:+.2f}", flush=True)
    for o_, cs, a, b, dlt in sorted(moved, key=lambda x: -abs(x[4])):
        print(f"  {o_:26s} {cs:9s} {a:6.2f} -> {b:6.2f}  {dlt:+.2f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
