"""910c A/B SDK: demo1+demo2 32-card (ab910.sh) and KTP (one op, one Job, npu=2)."""

from .client import AbClient
from .config import Config
from .job import AbLaunch, AbSpec, canon_op, guard_npu, guard_path
from .cases import catalog, diff_elapsed, parse_eval_file, s_of
from .layout import fill_demo1_ab, fill_demo2_ab, fill_fleet, fill_ktp_job, layout
from .parse import ArmSummary, RunLine, delta, parse_runs, summarize

__all__ = [
    "AbClient",
    "AbLaunch",
    "AbSpec",
    "ArmSummary",
    "Config",
    "RunLine",
    "canon_op",
    "catalog",
    "delta",
    "diff_elapsed",
    "fill_demo1_ab",
    "fill_demo2_ab",
    "fill_fleet",
    "fill_ktp_job",
    "guard_npu",
    "guard_path",
    "layout",
    "parse_eval_file",
    "parse_runs",
    "s_of",
    "summarize",
]
