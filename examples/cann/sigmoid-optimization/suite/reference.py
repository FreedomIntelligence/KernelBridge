import torch
class Model(torch.nn.Module):
    def forward(self,x): return torch.sigmoid(x.float()).to(x.dtype)
def get_inputs(): return [torch.randn(128,dtype=torch.float32)]
def get_init_inputs(): return []
