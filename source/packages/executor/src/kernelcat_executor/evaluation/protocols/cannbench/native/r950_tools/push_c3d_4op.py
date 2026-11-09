#!/usr/bin/env python3
"""4-op wall pack. Defaults to Conv3DBackpropFilter and remains backward compatible.

Usage:
  python3 push_c3d_4op.py /path/to/conv_3d_backprop_filter

For another wall-bound primary operator (2-op is known not to fit stage-3):
  CARRIER_PRIMARY_CAMEL=UnsortedSegmentSum CARRIER_PRIMARY_SNAKE=unsorted_segment_sum \
  CARRIER_NOTE=uss-rowscatter-4op python3 push_c3d_4op.py /path/to/unsorted_segment_sum

Optional carrier source overrides:
  C3D_TRANSPOSE_SOURCE=/path/to/transpose
  C3D_CUMMIN_SOURCE=/path/to/cummin
  C3D_ARGMAX_SOURCE=/path/to/arg_max
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

import wave as W

import gen_init
from push910 import capacity, fire_hidden, pick, rec, snapshot, wait_job

LIVE = HERE / "livesrc910"
PRIMARY_CAMEL = os.environ.get("CARRIER_PRIMARY_CAMEL", "Conv3DBackpropFilter")
PRIMARY_SNAKE = os.environ.get("CARRIER_PRIMARY_SNAKE", "conv_3d_backprop_filter")
PRIMARY_NOTE = os.environ.get("CARRIER_NOTE", "c3d-4op")


def source_override(env_name: str, fallback: Path) -> Path:
    raw = os.environ.get(env_name)
    return Path(raw).resolve() if raw else fallback


OPS = [
    (PRIMARY_CAMEL, None),  # filled from argv
    ("Transpose", source_override("C3D_TRANSPOSE_SOURCE", LIVE / "transpose")),
    ("Cummin", source_override("C3D_CUMMIN_SOURCE", LIVE / "cummin")),
    ("ArgMax", source_override("C3D_ARGMAX_SOURCE", LIVE / "arg_max")),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    primary = Path(sys.argv[1]).resolve()
    if not (primary / "op_plugin").is_dir():
        print(f"need {PRIMARY_SNAKE} tree with op_plugin/", file=sys.stderr)
        return 2
    ops = [(PRIMARY_CAMEL, primary)] + OPS[1:]
    for name, src in ops:
        if src is None or not src.is_dir():
            print(f"missing {name} {src}", file=sys.stderr)
            return 2

    tmpl = HERE.parent / "cann_submit" / "template"
    stage = tempfile.mkdtemp()
    pkg = os.path.join(stage, "pkg")
    shutil.copytree(tmpl, pkg)
    ops_dir = Path(pkg) / "csrc" / "ops"
    for d in ops_dir.iterdir():
        if d.is_dir():
            shutil.rmtree(d)
    snake = {
        PRIMARY_CAMEL: PRIMARY_SNAKE,
        "Transpose": "transpose",
        "Cummin": "cummin",
        "ArgMax": "arg_max",
    }
    for name, src in ops:
        dest = ops_dir / snake[name]
        shutil.copytree(
            src,
            dest,
            symlinks=False,
            ignore=shutil.ignore_patterns(
                "._*", "__pycache__", "*.pyc", "*.pyo",
                "verify_host.py", "*.md",
            ),
        )
    exported = gen_init.sync(pkg)
    want = set(snake.values())
    if not want.issubset(set(exported)):
        raise RuntimeError(f"cann_bench export {exported} missing {want - set(exported)}")
    print("cann_bench", exported)

    # Info-ZIP on Windows can leave a zero-byte target when antivirus briefly
    # holds the just-created archive, then fail while replacing it.  Python's
    # zipfile-backed archiver writes the same package without that rename race.
    zp = shutil.make_archive(os.path.join(stage, "s"), "zip", root_dir=pkg)

    cap = capacity()
    tv = pick(cap)
    if tv is None:
        print("no credit/slot")
        rec(kind="SKIP", op=PRIMARY_CAMEL, note=PRIMARY_NOTE, detail="no credit")
        return 1
    declared = [n for n, _ in ops]
    http, data = W.submit_zip(zp, declared, os.environ[tv], tv)
    shutil.rmtree(stage, ignore_errors=True)
    d = data if isinstance(data, dict) else {}
    jid = (d.get("job") or {}).get("id")
    sub = (d.get("submission") or {}).get("id")
    if not jid:
        print("submit fail", http, d)
        rec(kind="FAIL", op=PRIMARY_CAMEL, note=PRIMARY_NOTE, http=http, acct=tv)
        return 1
    rec(
        kind="SUBMIT",
        op=PRIMARY_CAMEL,
        note=PRIMARY_NOTE,
        job=jid,
        sub=sub,
        acct=tv,
        detail=f"{jid} ops={declared}",
    )
    print("submitted", jid, sub, tv, declared)

    m = wait_job(jid, tv, timeout=4500)
    st = m.get("status")
    sub = m.get("submission_id") or sub
    rec(kind="LAND", op=PRIMARY_CAMEL, note=PRIMARY_NOTE, detail=f"{jid} status={st}")
    print("LAND", st)
    if st != "succeeded":
        rec(kind="HIDDEN_SKIP", op=PRIMARY_CAMEL, note=PRIMARY_NOTE, detail=f"status={st}")
        tot, delta, moved = snapshot()
        print(f"board {tot:.2f} delta={delta:+.2f}")
        return 0
    hj, err = fire_hidden(sub, tv)
    if hj:
        rec(kind="HIDDEN", op=PRIMARY_CAMEL, note=PRIMARY_NOTE, detail=f"{hj} from={sub}")
        print("hidden", hj)
        hm = wait_job(hj, tv, timeout=4500)
        rec(kind="LAND_HIDDEN", op=PRIMARY_CAMEL, note=PRIMARY_NOTE,
            detail=f"{hj} status={hm.get('status')}")
        print("LAND_HIDDEN", hm.get("status"))
    else:
        rec(kind="HIDDEN_FAIL", op=PRIMARY_CAMEL, note=PRIMARY_NOTE, detail=str(err))
        print("hidden fail", err)
    tot, delta, moved = snapshot()
    print(f"board {tot:.2f} delta={delta:+.2f}")
    for o_, cs, a, b, dlt in sorted(moved, key=lambda x: -abs(x[4])):
        print(f"  {o_:26s} {cs:9s} {a:6.2f} -> {b:6.2f}  {dlt:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
