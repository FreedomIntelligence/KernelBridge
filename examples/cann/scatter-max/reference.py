import torch
SEEDS = [11, 23, 47, 89, 131]
_trial = 0
class Model(torch.nn.Module):
    def forward(self, src, index):
        # Independent grouped reduction: no torch_scatter dependency or candidate import.
        x = src.detach().cpu(); ids = index.detach().cpu()
        out = torch.zeros((33, 65), dtype=src.dtype)
        for destination in range(33):
            group = x[ids == destination]
            if group.shape[0]: out[destination] = group.amax(dim=0)
        return out.to(src.device)
def get_inputs():
    global _trial
    g = torch.Generator().manual_seed(SEEDS[_trial % len(SEEDS)]); _trial += 1
    return [torch.randn((129, 65), generator=g, dtype=torch.float32),
            torch.randint(33, (129,), generator=g, dtype=torch.int64)]
def get_init_inputs(): return []
