import torch
class ModelNew(torch.nn.Module):
    def forward(self, x): return torch.sigmoid(x)
