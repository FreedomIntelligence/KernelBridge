"""Frozen same-device eager baseline for the 128x128 FP16 Cube task.

The independent CPU FP32 oracle belongs to the driver, never this timed path.
"""


def matmul_eager(a, b):
    import torch

    if a.device.type != 'npu' or b.device != a.device:
        raise ValueError('Matmul eager requires both inputs on the measured NPU')
    return torch.matmul(a, b)
