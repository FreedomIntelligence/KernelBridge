"""Events must be recorded on the requested device stream, not the default NPU."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source/packages/executor/src'))
from kernelcat_executor.evaluation.measurement.ascend_events import time_execution_event_template


class EventDevice(unittest.TestCase):
    def test_explicit_device_stream_and_synchronization(self):
        selected = []; recorded = []; synchronized = []
        stream = object()
        class Model:
            def to(self, device):
                selected.append(device)
                return self
            def __call__(self):
                pass
        class Event:
            def __init__(self, **kwargs):
                pass
            def record(self, actual):
                recorded.append(actual)
            def elapsed_time(self, other):
                return 0.01
        def select(device):
            selected.append(device)
            return nullcontext()
        def current(device):
            selected.append(device)
            return stream
        npu = SimpleNamespace(device=select, current_stream=current)
        context = {'get_inputs':lambda:[], 'get_init_inputs':lambda:[], 'ModelNew':Model}
        with patch.object(torch, 'npu', npu, create=True):
            samples = time_execution_event_template(context, 'npu:1',
                lambda **kwargs:synchronized.append(kwargs['device']), Event, 'ModelNew',
                num_perf_trials=2, num_warmup=1)
        self.assertEqual(samples, [0.01, 0.01])
        self.assertEqual(recorded, [stream] * 4)
        self.assertEqual(selected, ['npu:1'] * 3)
        self.assertEqual(synchronized, ['npu:1'] * 3)
