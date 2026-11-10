
import torch
import custom_ops_lib

class ModelNew(torch.nn.Module):
    def forward(self, x, y):
        return custom_ops_lib.add(x, y)
