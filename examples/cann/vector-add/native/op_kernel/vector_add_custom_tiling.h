
#ifndef VECTOR_ADD_CUSTOM_TILING_H
#define VECTOR_ADD_CUSTOM_TILING_H
#include <cstdint>
struct VectorAddCustomTilingData {
    uint32_t size;     // total element count
    uint32_t tileLen;  // elements handled per UB tile (32B aligned)
    uint32_t tileNum;  // number of tiles
    uint32_t lastLen;  // elements in the last tile
};
#endif
