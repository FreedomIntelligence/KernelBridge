
#include "kernel_operator.h"
#include "sigmoid_custom_tiling.h"
using namespace AscendC;
template<class T, int DEPTH> class SigmoidKernel {
public:
    __aicore__ inline void Init(GM_ADDR x, GM_ADDR y, const SigmoidCustomTilingData& t) {
        uint32_t base = GetBlockIdx() * t.perCore;
        count = base < t.total ? (t.perCore < t.total - base ? t.perCore : t.total - base) : 0;
        tile = t.tile;
        xGm.SetGlobalBuffer((__gm__ T*)x + base, count);
        yGm.SetGlobalBuffer((__gm__ T*)y + base, count);
        pipe.InitBuffer(input, DEPTH, tile * sizeof(T));
        pipe.InitBuffer(output, DEPTH, tile * sizeof(T));
        pipe.InitBuffer(scratch, DEPTH * tile * sizeof(float));
        pipe.InitBuffer(onesBuffer, tile * sizeof(float));
    }
    __aicore__ inline void Process() {
        uint32_t nTile = (count + tile - 1) / tile;
        if (nTile == 0) { return; }
        for (uint32_t i = 0; i < nTile + DEPTH - 1; ++i) {
            if (i < nTile) { CopyIn(i); }
            if (i + 1 >= DEPTH) { Compute(i + 1 - DEPTH); }
        }
    }
private:
    __aicore__ inline uint32_t Len(uint32_t idx) const {
        uint32_t off = idx * tile;
        return tile < count - off ? tile : count - off;
    }
    __aicore__ inline void CopyIn(uint32_t idx) {
        LocalTensor<T> x = input.template AllocTensor<T>();
        DataCopyExtParams cp{1, static_cast<uint32_t>(Len(idx) * sizeof(T)), 0, 0, 0};
        DataCopyPadExtParams<T> pad{false, 0, 0, 0};
        DataCopyPad(x, xGm[idx * tile], cp, pad);
        input.EnQue(x);
    }
    __aicore__ inline void Compute(uint32_t idx) {
        uint32_t n = Len(idx);
        LocalTensor<T> x = input.template DeQue<T>();
        LocalTensor<float> f = scratch.Get<float>();
        if constexpr (sizeof(T) == sizeof(float)) {
            Muls(f, x.template ReinterpretCast<float>(), -1.0f, n);
        } else {
            Cast(f, x, RoundMode::CAST_NONE, n);
            PipeBarrier<PIPE_V>(); Muls(f, f, -1.0f, n);
        }
        PipeBarrier<PIPE_V>(); Exp(f, f, n);
        PipeBarrier<PIPE_V>(); Adds(f, f, 1.0f, n);
        LocalTensor<float> ones = onesBuffer.Get<float>();
        PipeBarrier<PIPE_V>(); Duplicate(ones, 1.0f, n);
        PipeBarrier<PIPE_V>(); Div(f, ones, f, n);
        PipeBarrier<PIPE_V>();
        LocalTensor<T> y = output.template AllocTensor<T>();
        if constexpr (sizeof(T) == sizeof(float)) {
            Muls(y.template ReinterpretCast<float>(), f, 1.0f, n);
        } else { Cast(y, f, RoundMode::CAST_RINT, n); }
        input.FreeTensor(x);
        output.EnQue(y);
        LocalTensor<T> o = output.template DeQue<T>();
        DataCopyExtParams cp{1, static_cast<uint32_t>(n * sizeof(T)), 0, 0, 0};
        DataCopyPad(yGm[idx * tile], o, cp);
        output.FreeTensor(o);
    }

    TPipe pipe;
    TQue<QuePosition::VECIN,DEPTH> input;
    TQue<QuePosition::VECOUT,DEPTH> output;
    TBuf<TPosition::VECCALC> scratch;
    TBuf<TPosition::VECCALC> onesBuffer;
    GlobalTensor<T> xGm, yGm;
    uint32_t count, tile;
};
extern "C" __global__ __aicore__ void sigmoid_custom(GM_ADDR x, GM_ADDR y, GM_ADDR workspace, GM_ADDR tiling) {
    REGISTER_TILING_DEFAULT(SigmoidCustomTilingData);
    GET_TILING_DATA(t, tiling);
    SigmoidKernel<DTYPE_X, 2> kernel;
    kernel.Init(x, y, t); kernel.Process();
}
