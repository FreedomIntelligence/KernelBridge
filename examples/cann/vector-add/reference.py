"""Frozen reference and input generator; generated from case.json."""
import torch

SHAPES = [[128], [128]]
SEEDS = [1024, 1025, 1026, 1027, 1028]
DTYPE = torch.float32
_trial = 0

class Model(torch.nn.Module):
    def forward(self, a, b):
        return a + b

def get_inputs():
    global _trial
    generator = torch.Generator(device='cpu').manual_seed(SEEDS[_trial % len(SEEDS)])
    _trial += 1
    return [torch.randn(shape, generator=generator, dtype=DTYPE) for shape in SHAPES]

def get_init_inputs():
    return []
