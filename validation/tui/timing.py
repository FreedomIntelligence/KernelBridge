"""Load-tolerant deadlines for the PTY scenarios.

Every scenario deadline is a base value times KB_TIMEOUT_SCALE (default 1), as
Node's platformTimeout and Playwright's test.slow() scale theirs. run.py sets
the scale from load average / CPUs and records it. A wait that expires raises
StepTimeout, so the runner can tell a timeout from an assertion failure.
"""
import os
import sys


def default_scale():
    """Scale for a run: load average per CPU, clamped to [1, 4]."""
    load = os.getloadavg()[0]
    return round(min(4.0, max(1.0, load / (os.cpu_count() or 1))), 2)


def scale():
    raw = os.environ.get('KB_TIMEOUT_SCALE')
    try:
        return max(1.0, float(raw)) if raw else 1.0
    except ValueError:
        return 1.0


SCALE = scale()


class StepTimeout(AssertionError):
    """A scaled wait expired. Reported as TIMEOUT, distinct from a failed assertion."""


def slow(message):
    print('SLOW: ' + message, file=sys.stderr, flush=True)
