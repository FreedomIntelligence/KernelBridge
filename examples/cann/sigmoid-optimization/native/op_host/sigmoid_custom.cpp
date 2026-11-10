
#include "../op_kernel/sigmoid_custom_tiling.h"
#include "register/op_def_registry.h"
namespace optiling {
static ge::graphStatus TilingFunc(gert::TilingContext* c) {
    auto* t = c->GetTilingData<SigmoidCustomTilingData>();
    t->total = static_cast<uint32_t>(c->GetInputShape(0)->GetStorageShape().GetShapeSize());
    constexpr uint32_t cores = 40;
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
