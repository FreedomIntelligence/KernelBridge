"""Shared logging configuration for evaluation entry points.

`evaluation.py` (parent) and `eval_single_runner.py` (child subprocess) both
call `setup_logging()` at module import time. The child subprocess's
`sys.stdout` is redirected by the parent (`run_eval_subprocess`) into a
per-op log file via `stdout=open(.../op.log)`; our `StreamHandler(sys.stdout)`
inherits that redirection so every logger.info/error call lands in the file
with the same format on both sides.

Format: `[YYYY-MM-DD HH:MM:SS.mmm] [LEVEL] message`
"""
import logging
import sys


_FORMAT = "[%(asctime)s.%(msecs)03d] [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: int = logging.INFO) -> None:
    """Idempotent — second call is a no-op."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    root.addHandler(handler)
    root.setLevel(level)
