"""Generate six-source Ascend C scatter max/min candidates (device validation pending).

The native contract is finite FP32 src[N,F], int64 index[N], dim=0, fixed
dim_size, values-only output. Output-cell ownership avoids atomic races and
provides a simple first incumbent; optimization should replace its O(B*N*F)
gather reduction after a passing original device receipt.
"""
import json


def sources(reduce, bins):
    if reduce not in ('max', 'min') or type(bins) is not int or bins < 0:
        raise ValueError('max/min and nonnegative bins required')
    name = 'Scatter' + reduce.capitalize() + 'Custom'
    entry = 'scatter_' + reduce + '_custom'
    header = entry + '_tiling.h'
    def fill(text):
        for key, value in {'OP_NAME': name, 'OP_ENTRY': entry, 'OP_HEADER': header,
                           'BIN_COUNT': str(bins), 'COMPARE': '>' if reduce == 'max' else '<'}.items():
            text = text.replace(key, value)
        return text
    project = [{'op': name, 'language': 'cpp', 'input_desc': [
        {'name': 'src', 'param_type': 'required', 'format': ['ND'], 'type': ['float']},
        {'name': 'index', 'param_type': 'required', 'format': ['ND'], 'type': ['int64']}],
        'output_desc': [{'name': 'out', 'param_type': 'required', 'format': ['ND'], 'type': ['float']}]}]
    tiling = '''#pragma once
#include <cstdint>
struct OP_NAMETilingData { uint32_t rows; uint32_t features; uint32_t bins; };
'''
    host = '''#include "../op_kernel/OP_HEADER"
#include "register/op_def_registry.h"
#include <climits>
namespace optiling {
static ge::graphStatus TilingFunc(gert::TilingContext* c) {
    const auto& s = c->GetInputShape(0)->GetStorageShape();
    const auto& i = c->GetInputShape(1)->GetStorageShape();
    if (s.GetDimNum() != 2 || i.GetDimNum() != 1 || i.GetDim(0) != s.GetDim(0)
        || s.GetDim(0) < 0 || s.GetDim(1) < 0 || s.GetShapeSize() > UINT_MAX
        || static_cast<uint64_t>(BIN_COUNT) * s.GetDim(1) > UINT_MAX) return ge::GRAPH_FAILED;
    auto* t = c->GetTilingData<OP_NAMETilingData>();
    t->rows = s.GetDim(0); t->features = s.GetDim(1); t->bins = BIN_COUNT;
    c->SetBlockDim(1); c->GetWorkspaceSizes(1)[0] = 0; return ge::GRAPH_SUCCESS;
}
}
namespace ge {
static ge::graphStatus InferShape(gert::InferShapeContext* c) {
    *c->GetOutputShape(0) = *c->GetInputShape(0); c->GetOutputShape(0)->SetDim(0, BIN_COUNT);
    return GRAPH_SUCCESS;
}
static ge::graphStatus InferDataType(gert::InferDataTypeContext* c) {
    c->SetOutputDataType(0, DT_FLOAT); return GRAPH_SUCCESS;
}
}
namespace ops {
class OP_NAME : public OpDef {
public:
    explicit OP_NAME(const char* name) : OpDef(name) {
        this->Input("src").ParamType(REQUIRED).DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
        this->Input("index").ParamType(REQUIRED).DataType({ge::DT_INT64}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
        this->Output("out").ParamType(REQUIRED).DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
        this->SetInferShape(ge::InferShape).SetInferDataType(ge::InferDataType);
        this->AICore().SetTiling(optiling::TilingFunc); this->AICore().AddConfig("ascend910_93");
    }
};
OP_ADD(OP_NAME);
}
'''
    kernel = '''#include "kernel_operator.h"
#include "OP_HEADER"
using namespace AscendC;
extern "C" __global__ __aicore__ void OP_ENTRY(GM_ADDR src, GM_ADDR index, GM_ADDR out, GM_ADDR workspace, GM_ADDR tiling) {
    REGISTER_TILING_DEFAULT(OP_NAMETilingData);
    GET_TILING_DATA(t, tiling);
    GlobalTensor<float> x, y; GlobalTensor<int64_t> ids;
    x.SetGlobalBuffer((__gm__ float*)src, t.rows * t.features);
    ids.SetGlobalBuffer((__gm__ int64_t*)index, t.rows);
    y.SetGlobalBuffer((__gm__ float*)out, t.bins * t.features);
    for (uint32_t offset = GetBlockIdx(); offset < t.bins * t.features; offset += GetBlockNum()) {
        uint32_t bin = offset / t.features, col = offset % t.features;
        float best = 0.0f; bool seen = false;
        for (uint32_t row = 0; row < t.rows; ++row) {
            if (ids.GetValue(row) == static_cast<int64_t>(bin)) {
                float value = x.GetValue(row * t.features + col);
                if (!seen || value COMPARE best) best = value;
                seen = true;
            }
        }
        y.SetValue(offset, best);
    }
}
'''
    binding = '''#include <torch/extension.h>
#include "pytorch_npu_helper.hpp"
at::Tensor run(const at::Tensor& src, const at::Tensor& index) {
    TORCH_CHECK(src.dim() == 2 && index.dim() == 1 && src.size(0) == index.size(0), "shape contract");
    TORCH_CHECK(src.scalar_type() == at::kFloat && index.scalar_type() == at::kLong, "dtype contract");
    TORCH_CHECK(src.is_contiguous() && index.is_contiguous(), "contiguous inputs required");
    auto out = at::empty({BIN_COUNT, src.size(1)}, src.options());
    EXEC_NPU_CMD(aclnnOP_NAME, src, index, out); return out;
}
PYBIND11_MODULE(custom_ops_lib, m) { m.def("run", &run); }
'''
    model = '''import torch
import custom_ops_lib
class ModelNew(torch.nn.Module):
    def forward(self, src, index):
        return custom_ops_lib.run(src, index)
'''
    return dict(project_json_src=json.dumps(project), host_tiling_src=fill(tiling), host_operator_src=fill(host),
                kernel_src=fill(kernel), python_bind_src=fill(binding), model_src=model)


def candidate(reduce, bins):
    return '\n'.join(name + ' = ' + repr(text) for name, text in sources(reduce, bins).items()) + '\n'
