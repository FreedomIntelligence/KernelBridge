
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
