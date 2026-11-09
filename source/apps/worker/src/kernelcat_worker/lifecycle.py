"""Process interruption enters the existing finite-effect cleanup path."""
from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

from kernelcat_executor.capabilities.worker import WorkerError


def _terminate(number: int, frame: FrameType | None) -> None:
    # A second termination must not interrupt reservation settlement and draining.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise WorkerError(f"worker interrupted by signal {number}; inspect receipts before resuming")


@contextmanager
def termination_boundary() -> Iterator[None]:
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
