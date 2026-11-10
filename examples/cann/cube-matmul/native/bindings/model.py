
import torch
import custom_ops_lib


class ModelNew(torch.nn.Module):
    def forward(self, a, b):
        return custom_ops_lib.matmul_custom(a, b)
