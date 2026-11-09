"""Small six-source native acceptance fixture; FP32 contiguous vector addition."""
import json
project_json_src = json.dumps([{
    'op': 'AuditAddCustom', 'language': 'cpp',
    'input_desc': [{'name': name, 'param_type': 'required', 'format': ['ND'], 'type': ['float']} for name in ('x', 'y')],
    'output_desc': [{'name': 'z', 'param_type': 'required', 'format': ['ND'], 'type': ['float']}],
}])
host_tiling_src = r'''
#ifndef AUDIT_ADD_CUSTOM_TILING_H
#define AUDIT_ADD_CUSTOM_TILING_H
#include <cstdint>
struct AuditAddCustomTilingData { uint32_t size; };
#endif
'''
host_operator_src = r'''
#include "../op_kernel/audit_add_custom_tiling.h"
#include "register/op_def_registry.h"
namespace optiling {
static ge::graphStatus TilingFunc(gert::TilingContext* c) {
 auto* t = c->GetTilingData<AuditAddCustomTilingData>();
 t->size = c->GetInputShape(0)->GetStorageShape().GetShapeSize();
 c->SetBlockDim(1); c->GetWorkspaceSizes(1)[0] = 0; return ge::GRAPH_SUCCESS;
}}
namespace ge {
static ge::graphStatus InferShape(gert::InferShapeContext* c) {
 *c->GetOutputShape(0) = *c->GetInputShape(0); return GRAPH_SUCCESS;
}
static ge::graphStatus InferDataType(gert::InferDataTypeContext* c) {
 c->SetOutputDataType(0, c->GetInputDataType(0)); return GRAPH_SUCCESS;
}}
namespace ops {
class AuditAddCustom: public OpDef {
public: explicit AuditAddCustom(const char* name): OpDef(name) {
 this->Input("x").ParamType(REQUIRED).DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
 this->Input("y").ParamType(REQUIRED).DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
 this->Output("z").ParamType(REQUIRED).DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
 this->SetInferShape(ge::InferShape).SetInferDataType(ge::InferDataType);
 this->AICore().SetTiling(optiling::TilingFunc); this->AICore().AddConfig("ascend910_93");
}};
OP_ADD(AuditAddCustom);
}
'''
kernel_src = r'''
#include "kernel_operator.h"
#include "audit_add_custom_tiling.h"
extern "C" __global__ __aicore__ void audit_add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR workspace, GM_ADDR tiling) {
 REGISTER_TILING_DEFAULT(AuditAddCustomTilingData);
 GET_TILING_DATA(t, tiling);
 AscendC::GlobalTensor<float> a,b,c;
 a.SetGlobalBuffer((__gm__ float*)x, t.size);
 b.SetGlobalBuffer((__gm__ float*)y, t.size);
 c.SetGlobalBuffer((__gm__ float*)z, t.size);
 for(uint32_t i=0;i<t.size;++i) c.SetValue(i,a.GetValue(i)+b.GetValue(i));
}
'''
python_bind_src = r'''
#include <torch/extension.h>
#include "pytorch_npu_helper.hpp"
at::Tensor add(const at::Tensor& x, const at::Tensor& y) {
 auto z=at::empty_like(x); EXEC_NPU_CMD(aclnnAuditAddCustom, x, y, z); return z;
}
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) { m.def("add", &add); }
'''
model_src = '''
import torch
import custom_ops_lib
class ModelNew(torch.nn.Module):
    def forward(self, x, y): return custom_ops_lib.add(x, y)
'''
