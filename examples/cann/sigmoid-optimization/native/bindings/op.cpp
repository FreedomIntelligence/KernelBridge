
#include <torch/extension.h>
#include "pytorch_npu_helper.hpp"
at::Tensor sigmoid(const at::Tensor& x) {
    auto y = at::empty_like(x); EXEC_NPU_CMD(aclnnSigmoidCustom, x, y); return y;
}
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) { m.def("sigmoid", &sigmoid); }
