
import torch
import custom_ops_lib
class ModelNew(torch.nn.Module):
    def forward(self, x): return custom_ops_lib.sigmoid(x)
