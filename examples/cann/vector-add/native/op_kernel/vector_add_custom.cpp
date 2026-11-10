
#include "kernel_operator.h"
#include "vector_add_custom_tiling.h"

using namespace AscendC;

class VectorAddKernel {
public:
    __aicore__ inline VectorAddKernel() {}

    __aicore__ inline void Init(GM_ADDR x, GM_ADDR y, GM_ADDR z, const VectorAddCustomTilingData& t) {
        xGm_.SetGlobalBuffer((__gm__ float*)x, t.size);
        yGm_.SetGlobalBuffer((__gm__ float*)y, t.size);
        zGm_.SetGlobalBuffer((__gm__ float*)z, t.size);
        pipe_.InitBuffer(inQx_, 1, t.tileLen * sizeof(float));
        pipe_.InitBuffer(inQy_, 1, t.tileLen * sizeof(float));
        pipe_.InitBuffer(outQ_, 1, t.tileLen * sizeof(float));
        size_ = t.size;
        tileLen_ = t.tileLen;
    }

    __aicore__ inline void Process() {
        uint32_t off = 0;
        while (off < size_) {
            uint32_t n = size_ - off;
            if (n > tileLen_) n = tileLen_;
            CopyAdd(off, n);
            off += n;
        }
    }

private:
    __aicore__ inline void CopyAdd(uint32_t srcOff, uint32_t n) {
        LocalTensor<float> lx = inQx_.AllocTensor<float>();
        LocalTensor<float> ly = inQy_.AllocTensor<float>();
        LocalTensor<float> lz = outQ_.AllocTensor<float>();
        const uint32_t aligned = n & ~7u;
        const uint32_t tail = n - aligned;
        if (aligned > 0) {
            DataCopy(lx, xGm_[srcOff], aligned);
            DataCopy(ly, yGm_[srcOff], aligned);
        }
        if (tail > 0) {
            DataCopyExtParams tp{1, static_cast<uint32_t>(tail * sizeof(float)), 0, 0, 0};
            DataCopyPadExtParams<float> padParams{false, 0, 0, 0.0f};
            DataCopyPad(lx[aligned], xGm_[srcOff + aligned], tp, padParams);
            DataCopyPad(ly[aligned], yGm_[srcOff + aligned], tp, padParams);
        }
        inQx_.EnQue(lx);
        inQy_.EnQue(ly);
        lx = inQx_.DeQue<float>();
        ly = inQy_.DeQue<float>();
        Add(lz, lx, ly, n);
        outQ_.EnQue(lz);
        lz = outQ_.DeQue<float>();
        if (aligned > 0) {
            DataCopy(zGm_[srcOff], lz, aligned);
        }
        if (tail > 0) {
            DataCopyExtParams tp{1, static_cast<uint32_t>(tail * sizeof(float)), 0, 0, 0};
            DataCopyPad(zGm_[srcOff + aligned], lz[aligned], tp);
        }
        inQx_.FreeTensor(lx);
        inQy_.FreeTensor(ly);
        outQ_.FreeTensor(lz);
    }

    TPipe pipe_;
    TQue<QuePosition::VECIN, 1> inQx_;
    TQue<QuePosition::VECIN, 1> inQy_;
    TQue<QuePosition::VECOUT, 1> outQ_;
    GlobalTensor<float> xGm_, yGm_, zGm_;
    uint32_t size_ = 0;
    uint32_t tileLen_ = 0;
};

extern "C" __global__ __aicore__ void vector_add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR workspace, GM_ADDR tiling) {
    REGISTER_TILING_DEFAULT(VectorAddCustomTilingData);
    GET_TILING_DATA(t, tiling);
    VectorAddKernel k;
    k.Init(x, y, z, t);
    k.Process();
}
