"""All-NPU Torch eager composition for finite FP32 index-group max/min values.

This is explicitly a broadcast-mask reduction baseline, not torch_scatter's
extension or the unsupported aten::scatter_reduce NPU implementation.
"""


def scatter_eager(src, index, bins, reduce):
    import torch
    if reduce not in ('max', 'min') or src.device.type != 'npu' or index.device != src.device:
        raise ValueError('max/min and same-device NPU inputs required')
    if not src.shape[0]:
        return torch.zeros((bins, src.shape[1]), dtype=src.dtype, device=src.device)
    mask = torch.arange(bins, device=src.device, dtype=index.dtype)[:, None] == index[None, :]
    values = torch.where(mask[:, :, None], src[None, :, :], -float('inf') if reduce == 'max' else float('inf'))
    reduced = values.amax(dim=1) if reduce == 'max' else values.amin(dim=1)
    return torch.where(mask.any(dim=1)[:, None], reduced, 0.0)
