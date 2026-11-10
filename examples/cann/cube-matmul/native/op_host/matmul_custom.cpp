
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
