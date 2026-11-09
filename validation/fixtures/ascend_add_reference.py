import torch
class Model(torch.nn.Module):
    def forward(self, x, y): return x + y
def get_inputs(): return [torch.randn(128), torch.randn(128)]
def get_init_inputs(): return []
