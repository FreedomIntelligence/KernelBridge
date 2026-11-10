
#include <torch/extension.h>
#include "pytorch_npu_helper.hpp"
at::Tensor add(const at::Tensor& x, const at::Tensor& y) {
    auto z = at::empty_like(x);
    EXEC_NPU_CMD(aclnnVectorAddCustom, x, y, z);
    return z;
}
PYBIND11_MODULE(custom_ops_lib, m) { m.def("add", &add); }
