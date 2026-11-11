// Portable FP32 feature-index scatter max/min. Every input row contributes to
// one destination row. No torch, no input mutation, no atomics on the CPU path.
#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

extern "C" int scatter_extrema(const float* src, const int64_t* index,
                               int64_t rows, int64_t features, int64_t bins,
                               int maximum, float* output) {
    if (rows < 0 || features < 0 || bins < 0 || (maximum != 0 && maximum != 1)
        || (bins && features > INT64_MAX / bins)
        || (rows && features > INT64_MAX / rows)) return 1;
    for (int64_t row = 0; row < rows; ++row)
        if (index[row] < 0 || index[row] >= bins) return 2;
    for (int64_t i = 0; i < rows * features; ++i)
        if (!std::isfinite(src[i]) || std::abs(src[i]) >= std::numeric_limits<float>::max()) return 3;
    std::vector<unsigned char> seen(static_cast<size_t>(bins), 0);
    for (int64_t i = 0; i < bins * features; ++i) output[i] = 0;
    for (int64_t row = 0; row < rows; ++row) {
        const int64_t bin = index[row];
        for (int64_t col = 0; col < features; ++col) {
            float value = src[row * features + col];
            float& slot = output[bin * features + col];
            if (!seen[bin] || (maximum ? value > slot : value < slot)) slot = value;
        }
        seen[bin] = 1;
    }
    return 0;
}

extern "C" int scatter_max(const float* x, const int64_t* i, int64_t n, int64_t f, int64_t b, float* y) {
    return scatter_extrema(x, i, n, f, b, 1, y);
}
extern "C" int scatter_min(const float* x, const int64_t* i, int64_t n, int64_t f, int64_t b, float* y) {
    return scatter_extrema(x, i, n, f, b, 0, y);
}
