"""Conservative one-core native seed for a separately labelled optimization trial.

Hand-authored from the verified Vector queue pattern. It is not a model-generated
solution or a performance claim; the full CANNBench suite must qualify it first.
"""
import json

project_json_src = json.dumps([{
    'op': 'SigmoidCustom', 'language': 'cpp',
    'input_desc': [{'name': 'x', 'param_type': 'required', 'format': ['ND'],
                    'type': ['float16', 'float', 'bfloat16']}],
    'output_desc': [{'name': 'y', 'param_type': 'required', 'format': ['ND'],
                     'type': ['float16', 'float', 'bfloat16']}],
}])

host_tiling_src = r'''
#ifndef SIGMOID_CUSTOM_TILING_H
#define SIGMOID_CUSTOM_TILING_H
#include <cstdint>
struct SigmoidCustomTilingData { uint32_t total; uint32_t perCore; uint32_t tile; };
#endif
'''

host_operator_src = r'''
#include "../op_kernel/sigmoid_custom_tiling.h"
#include "register/op_def_registry.h"
namespace optiling {
static ge::graphStatus TilingFunc(gert::TilingContext* c) {
    auto* t = c->GetTilingData<SigmoidCustomTilingData>();
    t->total = static_cast<uint32_t>(c->GetInputShape(0)->GetStorageShape().GetShapeSize());
    constexpr uint32_t cores = 1;
    t->perCore = ((t->total + cores - 1) / cores + 15) / 16 * 16;
    t->tile = 2048;
    c->SetBlockDim(cores);
    c->GetWorkspaceSizes(1)[0] = 0;
    return ge::GRAPH_SUCCESS;
}
}
namespace ge {
static ge::graphStatus InferShape(gert::InferShapeContext* c) {
    *c->GetOutputShape(0) = *c->GetInputShape(0); return GRAPH_SUCCESS;
}
static ge::graphStatus InferDataType(gert::InferDataTypeContext* c) {
    c->SetOutputDataType(0, c->GetInputDataType(0)); return GRAPH_SUCCESS;
}
}
namespace ops {
class SigmoidCustom : public OpDef {
public:
    explicit SigmoidCustom(const char* name) : OpDef(name) {
        this->Input("x").ParamType(REQUIRED).DataType({ge::DT_FLOAT16,ge::DT_FLOAT,ge::DT_BF16})
            .Format({ge::FORMAT_ND,ge::FORMAT_ND,ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND,ge::FORMAT_ND,ge::FORMAT_ND});
        this->Output("y").ParamType(REQUIRED).DataType({ge::DT_FLOAT16,ge::DT_FLOAT,ge::DT_BF16})
            .Format({ge::FORMAT_ND,ge::FORMAT_ND,ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND,ge::FORMAT_ND,ge::FORMAT_ND});
        this->SetInferShape(ge::InferShape).SetInferDataType(ge::InferDataType);
        this->AICore().SetTiling(optiling::TilingFunc).AddConfig("ascend910_93");
    }
};
OP_ADD(SigmoidCustom);
}
'''

kernel_src = r'''
#include "kernel_operator.h"
#include "sigmoid_custom_tiling.h"
using namespace AscendC;
template<class T> class SigmoidKernel {
public:
    __aicore__ inline void Init(GM_ADDR x, GM_ADDR y, const SigmoidCustomTilingData& t) {
        uint32_t base = GetBlockIdx() * t.perCore;
        count = base < t.total ? (t.perCore < t.total - base ? t.perCore : t.total - base) : 0;
        tile = t.tile;
        xGm.SetGlobalBuffer((__gm__ T*)x + base, count);
        yGm.SetGlobalBuffer((__gm__ T*)y + base, count);
        pipe.InitBuffer(input, 1, tile * sizeof(T));
        pipe.InitBuffer(output, 1, tile * sizeof(T));
        pipe.InitBuffer(scratch, tile * sizeof(float));
        pipe.InitBuffer(onesBuffer, tile * sizeof(float));
    }
    __aicore__ inline void Process() {
        for (uint32_t off = 0; off < count; off += tile) {
            uint32_t n = tile < count - off ? tile : count - off;
            LocalTensor<T> x = input.AllocTensor<T>();
            DataCopyExtParams cp{1, static_cast<uint32_t>(n * sizeof(T)), 0, 0, 0};
            DataCopyPadExtParams<T> pad{false, 0, 0, 0};
            DataCopyPad(x, xGm[off], cp, pad);
            input.EnQue(x); x = input.DeQue<T>();
            LocalTensor<float> f = scratch.Get<float>();
            if constexpr (sizeof(T) == sizeof(float)) {
                Muls(f, x.template ReinterpretCast<float>(), -1.0f, n);
            } else {
                Cast(f, x, RoundMode::CAST_NONE, n);
                PipeBarrier<PIPE_V>(); Muls(f, f, -1.0f, n);
            }
            PipeBarrier<PIPE_V>(); Exp(f, f, n);
            PipeBarrier<PIPE_V>(); Adds(f, f, 1.0f, n);
            LocalTensor<float> ones = onesBuffer.Get<float>();
            PipeBarrier<PIPE_V>(); Duplicate(ones, 1.0f, n);
            PipeBarrier<PIPE_V>(); Div(f, ones, f, n);
            PipeBarrier<PIPE_V>();
            LocalTensor<T> y = output.AllocTensor<T>();
            if constexpr (sizeof(T) == sizeof(float)) {
                Muls(y.template ReinterpretCast<float>(), f, 1.0f, n);
            } else { Cast(y, f, RoundMode::CAST_RINT, n); }
            output.EnQue(y); y = output.DeQue<T>();
            DataCopyPad(yGm[off], y, cp);
            input.FreeTensor(x); output.FreeTensor(y);
        }
    }
private:
    TPipe pipe;
    TQue<QuePosition::VECIN,1> input;
    TQue<QuePosition::VECOUT,1> output;
    TBuf<TPosition::VECCALC> scratch;
    TBuf<TPosition::VECCALC> onesBuffer;
    GlobalTensor<T> xGm, yGm;
    uint32_t count, tile;
};
extern "C" __global__ __aicore__ void sigmoid_custom(GM_ADDR x, GM_ADDR y, GM_ADDR workspace, GM_ADDR tiling) {
    REGISTER_TILING_DEFAULT(SigmoidCustomTilingData);
    GET_TILING_DATA(t, tiling);
    SigmoidKernel<DTYPE_X> kernel;
    kernel.Init(x, y, t); kernel.Process();
}
'''

python_bind_src = r'''
#include <torch/extension.h>
#include "pytorch_npu_helper.hpp"
at::Tensor sigmoid(const at::Tensor& x) {
    auto y = at::empty_like(x); EXEC_NPU_CMD(aclnnSigmoidCustom, x, y); return y;
}
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) { m.def("sigmoid", &sigmoid); }
'''

model_src = '''
import torch
import custom_ops_lib
class ModelNew(torch.nn.Module):
    def forward(self, x): return custom_ops_lib.sigmoid(x)
'''
