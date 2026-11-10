"""Six-source candidate: Cube matmul C = A @ B (no transpose, no bias), FP32 accum, FP16 out."""
import json

project_json_src = json.dumps([{
    'op': 'MatmulCustom', 'language': 'cpp',
    'input_desc': [
        {'name': 'a', 'param_type': 'required', 'format': ['ND'], 'type': ['float16']},
        {'name': 'b', 'param_type': 'required', 'format': ['ND'], 'type': ['float16']},
    ],
    'output_desc': [{'name': 'c', 'param_type': 'required', 'format': ['ND'], 'type': ['float16']}],
}])

host_tiling_src = r'''
#ifndef MATMUL_CUSTOM_TILING_H
#define MATMUL_CUSTOM_TILING_H
#include "register/tilingdata_base.h"
#include "tiling/tiling_api.h"

namespace optiling {
BEGIN_TILING_DATA_DEF(MatmulCustomTilingData)
TILING_DATA_FIELD_DEF_STRUCT(TCubeTiling, cubeTiling);
END_TILING_DATA_DEF;

REGISTER_TILING_DATA_CLASS(MatmulCustom, MatmulCustomTilingData)
}  // namespace optiling

// Host-side mirror of the registered tiling struct for the kernel-side header copy.
struct MatmulCustomTilingData { TCubeTiling cubeTiling; };
#endif
'''

host_operator_src = r'''
#include <cstdint>
#include "tiling/platform/platform_ascendc.h"
#include "tiling/tiling_api.h"
#include "../op_kernel/matmul_custom_tiling.h"
#include "register/op_def_registry.h"

namespace optiling {
static ge::graphStatus TilingFunc(gert::TilingContext* c) {
  MatmulCustomTilingData tilingData;
  optiling::TCubeTiling& cubeTiling = tilingData.cubeTiling;

  const gert::StorageShape* aShape = c->GetInputShape(0);
  const gert::StorageShape* bShape = c->GetInputShape(1);
  int32_t M = static_cast<int32_t>(aShape->GetStorageShape().GetDim(0));
  int32_t K = static_cast<int32_t>(aShape->GetStorageShape().GetDim(1));
  int32_t N = static_cast<int32_t>(bShape->GetStorageShape().GetDim(1));

  platform_ascendc::PlatformAscendC platform(c->GetPlatformInfo());
  matmul_tiling::MatmulApiTiling mmTiling(platform);
  mmTiling.SetAType(matmul_tiling::TPosition::GM, matmul_tiling::CubeFormat::ND,
                    matmul_tiling::DataType::DT_FLOAT16, false);
  mmTiling.SetBType(matmul_tiling::TPosition::GM, matmul_tiling::CubeFormat::ND,
                    matmul_tiling::DataType::DT_FLOAT16, false);
  mmTiling.SetCType(matmul_tiling::TPosition::GM, matmul_tiling::CubeFormat::ND,
                    matmul_tiling::DataType::DT_FLOAT16);
  mmTiling.SetBias(false);
  mmTiling.SetShape(M, N, K);
  mmTiling.SetOrgShape(M, N, K);
  mmTiling.SetFixSplit(M, N, K);

  if (mmTiling.GetTiling(cubeTiling) == -1) {
    return ge::GRAPH_FAILED;
  }

  cubeTiling.set_usedCoreNum(1);
  cubeTiling.set_singleCoreM(M);
  cubeTiling.set_singleCoreN(N);
  cubeTiling.set_singleCoreK(K);

  tilingData.SaveToBuffer(c->GetRawTilingData()->GetData(),
                          c->GetRawTilingData()->GetCapacity());
  c->GetRawTilingData()->SetDataSize(tilingData.GetDataSize());

  c->SetBlockDim(1);
  c->GetWorkspaceSizes(1)[0] =
      static_cast<size_t>(platform.GetLibApiWorkSpaceSize());
  return ge::GRAPH_SUCCESS;
}
}  // namespace optiling

namespace ge {
static ge::graphStatus InferShape(gert::InferShapeContext* c) {
  auto aShape = *c->GetInputShape(0);
  auto bShape = *c->GetInputShape(1);
  gert::Shape* outShape = c->GetOutputShape(0);
  outShape->SetDimNum(2);
  outShape->SetDim(0, aShape.GetDim(0));
  outShape->SetDim(1, bShape.GetDim(1));
  return GRAPH_SUCCESS;
}
static ge::graphStatus InferDataType(gert::InferDataTypeContext* c) {
  c->SetOutputDataType(0, c->GetInputDataType(0));
  return GRAPH_SUCCESS;
}
}  // namespace ge

namespace ops {
class MatmulCustom : public OpDef {
 public:
  explicit MatmulCustom(const char* name) : OpDef(name) {
    this->Input("a").ParamType(REQUIRED).DataType({ge::DT_FLOAT16}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
    this->Input("b").ParamType(REQUIRED).DataType({ge::DT_FLOAT16}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
    this->Output("c").ParamType(REQUIRED).DataType({ge::DT_FLOAT16}).Format({ge::FORMAT_ND}).UnknownShapeFormat({ge::FORMAT_ND});
    this->SetInferShape(ge::InferShape).SetInferDataType(ge::InferDataType);
    this->AICore().SetTiling(optiling::TilingFunc);
    this->AICore().AddConfig("ascend910_93");
  }
};
OP_ADD(MatmulCustom);
}  // namespace ops
'''

kernel_src = r'''
#define ASCENDC_CUBE_ONLY
#include "kernel_operator.h"
#include "kernel_tiling/kernel_tiling.h"
#include "lib/matmul_intf.h"

using namespace AscendC;

extern "C" __global__ __aicore__ void matmul_custom(GM_ADDR a, GM_ADDR b, GM_ADDR c,
                                                    GM_ADDR workspace, GM_ADDR tiling) {
  GET_TILING_DATA(tilingData, tiling);
  const TCubeTiling* cubeTiling = &tilingData.cubeTiling;

  if (GetSysWorkSpacePtr() == nullptr) {
    return;
  }

  using A_T = MatmulType<TPosition::GM, CubeFormat::ND, half, false>;
  using B_T = MatmulType<TPosition::GM, CubeFormat::ND, half, false>;
  using C_T = MatmulType<TPosition::GM, CubeFormat::ND, half, false>;
  using BIAS_T = MatmulType<TPosition::GM, CubeFormat::ND, float, false>;

  TPipe pipe;
  Matmul<A_T, B_T, C_T, BIAS_T> mm;
  REGIST_MATMUL_OBJ(&pipe, GetSysWorkSpacePtr(), mm, const_cast<TCubeTiling*>(cubeTiling));

  GlobalTensor<half> aGm;
  GlobalTensor<half> bGm;
  GlobalTensor<half> cGm;
  aGm.SetGlobalBuffer((__gm__ half*)a);
  bGm.SetGlobalBuffer((__gm__ half*)b);
  cGm.SetGlobalBuffer((__gm__ half*)c);

  mm.SetTensorA(aGm, false);
  mm.SetTensorB(bGm, false);
  mm.IterateAll(cGm);
  mm.End();
}
'''

python_bind_src = r'''
#include <torch/extension.h>
#include "pytorch_npu_helper.hpp"

at::Tensor matmul_custom(const at::Tensor& a, const at::Tensor& b) {
  auto c = at::empty({a.size(0), b.size(1)}, a.options());
  EXEC_NPU_CMD(aclnnMatmulCustom, a, b, c);
  return c;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) { m.def("matmul_custom", &matmul_custom); }
'''

model_src = '''
import torch
import custom_ops_lib


class ModelNew(torch.nn.Module):
    def forward(self, a, b):
        return custom_ops_lib.matmul_custom(a, b)
'''
