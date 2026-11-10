
#include <torch/extension.h>
#include "pytorch_npu_helper.hpp"

at::Tensor matmul_custom(const at::Tensor& a, const at::Tensor& b) {
  auto c = at::empty({a.size(0), b.size(1)}, a.options());
  EXEC_NPU_CMD(aclnnMatmulCustom, a, b, c);
  return c;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) { m.def("matmul_custom", &matmul_custom); }
