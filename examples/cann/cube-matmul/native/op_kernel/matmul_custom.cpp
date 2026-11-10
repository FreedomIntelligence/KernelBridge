
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
