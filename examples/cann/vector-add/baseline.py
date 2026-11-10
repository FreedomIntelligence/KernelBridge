"""Six-source Ascend C candidate: fp32 vector addition c = a + b using the Vector unit."""
import json

project_json_src = json.dumps([{
    'op': 'VectorAddCustom', 'language': 'cpp',
    'input_desc': [{'name': name, 'param_type': 'required', 'format': ['ND'], 'type': ['float']} for name in ('x', 'y')],
    'output_desc': [{'name': 'z', 'param_type': 'required', 'format': ['ND'], 'type': ['float']}],
}])

host_tiling_src = r'''
#ifndef VECTOR_ADD_CUSTOM_TILING_H
#define VECTOR_ADD_CUSTOM_TILING_H
#include <cstdint>
struct VectorAddCustomTilingData {
    uint32_t size;     // total element count
    uint32_t tileLen;  // elements handled per UB tile (32B aligned)
    uint32_t tileNum;  // number of tiles
    uint32_t lastLen;  // elements in the last tile
};
#endif
'''

host_operator_src = r'''
#include "../op_kernel/vector_add_custom_tiling.h"
#include "register/op_def_registry.h"

namespace optiling {
static ge::graphStatus TilingFunc(gert::TilingContext* c) {
    auto* t = c->GetTilingData<VectorAddCustomTilingData>();
    uint32_t size = static_cast<uint32_t>(c->GetInputShape(0)->GetStorageShape().GetShapeSize());
    constexpr uint32_t kAlign = 8;  // 32B / sizeof(float)
    uint32_t tileLen = 8192;
    if (tileLen > size) tileLen = size;
    tileLen = (tileLen / kAlign) * kAlign;
    if (tileLen == 0) tileLen = kAlign;
    if (tileLen > size) tileLen = size;
    uint32_t tileNum = (size + tileLen - 1) / tileLen;
    if (tileNum == 0) { tileLen = kAlign; tileNum = 1; }
    t->size = size;
    t->tileLen = tileLen;
    t->tileNum = tileNum;
    t->lastLen = size - (tileNum - 1) * tileLen;
    c->SetBlockDim(1);
    c->GetWorkspaceSizes(1)[0] = 0;
    return ge::GRAPH_SUCCESS;
}
}

namespace ge {
static ge::graphStatus InferShape(gert::InferShapeContext* c) {
    *c->GetOutputShape(0) = *c->GetInputShape(0);
    return GRAPH_SUCCESS;
}
static ge::graphStatus InferDataType(gert::InferDataTypeContext* c) {
    c->SetOutputDataType(0, c->GetInputDataType(0));
    return GRAPH_SUCCESS;
}
}

namespace ops {
class VectorAddCustom : public OpDef {
public:
    explicit VectorAddCustom(const char* name) : OpDef(name) {
        this->Input("x").ParamType(REQUIRED).DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
        this->Input("y").ParamType(REQUIRED).DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
        this->Output("z").ParamType(REQUIRED).DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
        this->SetInferShape(ge::InferShape).SetInferDataType(ge::InferDataType);
        this->AICore().SetTiling(optiling::TilingFunc);
        this->AICore().AddConfig("ascend910_93");
    }
};
OP_ADD(VectorAddCustom);
}
'''

kernel_src = r'''
#include "kernel_operator.h"
#include "vector_add_custom_tiling.h"

using namespace AscendC;

class VectorAddKernel {
public:
    __aicore__ inline VectorAddKernel() {}

    __aicore__ inline void Init(GM_ADDR x, GM_ADDR y, GM_ADDR z, const VectorAddCustomTilingData& t) {
        xGm_.SetGlobalBuffer((__gm__ float*)x, t.size);
        yGm_.SetGlobalBuffer((__gm__ float*)y, t.size);
        zGm_.SetGlobalBuffer((__gm__ float*)z, t.size);
        pipe_.InitBuffer(inQx_, 1, t.tileLen * sizeof(float));
        pipe_.InitBuffer(inQy_, 1, t.tileLen * sizeof(float));
        pipe_.InitBuffer(outQ_, 1, t.tileLen * sizeof(float));
        size_ = t.size;
        tileLen_ = t.tileLen;
    }

    __aicore__ inline void Process() {
        uint32_t off = 0;
        while (off < size_) {
            uint32_t n = size_ - off;
            if (n > tileLen_) n = tileLen_;
            CopyAdd(off, n);
            off += n;
        }
    }

private:
    __aicore__ inline void CopyAdd(uint32_t srcOff, uint32_t n) {
        LocalTensor<float> lx = inQx_.AllocTensor<float>();
        LocalTensor<float> ly = inQy_.AllocTensor<float>();
        LocalTensor<float> lz = outQ_.AllocTensor<float>();
        const uint32_t aligned = n & ~7u;
        const uint32_t tail = n - aligned;
        if (aligned > 0) {
            DataCopy(lx, xGm_[srcOff], aligned);
            DataCopy(ly, yGm_[srcOff], aligned);
        }
        if (tail > 0) {
            DataCopyExtParams tp{1, static_cast<uint32_t>(tail * sizeof(float)), 0, 0, 0};
            DataCopyPadExtParams<float> padParams{false, 0, 0, 0.0f};
            DataCopyPad(lx[aligned], xGm_[srcOff + aligned], tp, padParams);
            DataCopyPad(ly[aligned], yGm_[srcOff + aligned], tp, padParams);
        }
        inQx_.EnQue(lx);
        inQy_.EnQue(ly);
        lx = inQx_.DeQue<float>();
        ly = inQy_.DeQue<float>();
        Add(lz, lx, ly, n);
        outQ_.EnQue(lz);
        lz = outQ_.DeQue<float>();
        if (aligned > 0) {
            DataCopy(zGm_[srcOff], lz, aligned);
        }
        if (tail > 0) {
            DataCopyExtParams tp{1, static_cast<uint32_t>(tail * sizeof(float)), 0, 0, 0};
            DataCopyPad(zGm_[srcOff + aligned], lz[aligned], tp);
        }
        inQx_.FreeTensor(lx);
        inQy_.FreeTensor(ly);
        outQ_.FreeTensor(lz);
    }

    TPipe pipe_;
    TQue<QuePosition::VECIN, 1> inQx_;
    TQue<QuePosition::VECIN, 1> inQy_;
    TQue<QuePosition::VECOUT, 1> outQ_;
    GlobalTensor<float> xGm_, yGm_, zGm_;
    uint32_t size_ = 0;
    uint32_t tileLen_ = 0;
};

extern "C" __global__ __aicore__ void vector_add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR workspace, GM_ADDR tiling) {
    REGISTER_TILING_DEFAULT(VectorAddCustomTilingData);
    GET_TILING_DATA(t, tiling);
    VectorAddKernel k;
    k.Init(x, y, z, t);
    k.Process();
}
'''

python_bind_src = r'''
#include <torch/extension.h>
#include "pytorch_npu_helper.hpp"
at::Tensor add(const at::Tensor& x, const at::Tensor& y) {
    auto z = at::empty_like(x);
    EXEC_NPU_CMD(aclnnVectorAddCustom, x, y, z);
    return z;
}
PYBIND11_MODULE(custom_ops_lib, m) { m.def("add", &add); }
'''

model_src = '''
import torch
import custom_ops_lib

class ModelNew(torch.nn.Module):
    def forward(self, x, y):
        return custom_ops_lib.add(x, y)
'''
