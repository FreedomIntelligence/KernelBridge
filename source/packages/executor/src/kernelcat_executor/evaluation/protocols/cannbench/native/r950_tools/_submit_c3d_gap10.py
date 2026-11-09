#!/usr/bin/env python3
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load_cann_env

_load_cann_env.load()
os.environ["PYTHONUNBUFFERED"] = "1"

arm = HERE.parent.parent / "910c_tasks" / "conv_3d_backprop_filter" / "arm"
sys.argv = ["push_c3d_2op.py", str(arm)]
import push_c3d_2op

raise SystemExit(push_c3d_2op.main())
