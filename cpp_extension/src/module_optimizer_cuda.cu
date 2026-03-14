#include <cuda_runtime.h>
#include <device_launch_parameters.h>
#include <cstdio>
#include <algorithm>
#include <vector>
#include <queue>
#include <cub/cub.cuh>

// GPU 配置信息
struct GpuConfig {
    int max_threads_per_block;
    int max_blocks_per_sm;
    int multiprocessor_count;
    int max_grid_size;
    size_t global_memory;
    int compute_capability_major;
    int compute_capability_minor;
    int optimal_block_size;
    int optimal_grid_size;
    long long optimal_batch_size;
};

__constant__ int D_ATTR_THRESHOLDS[6]    = {1, 4, 8, 12, 16, 20};
__constant__ int D_BASIC_POWER_VALUES[6]  = {7, 14, 29, 44, 167, 254};
__constant__ int D_SPECIAL_POWER_VALUES[6]= {14, 29, 59, 89, 298, 448};
__constant__ int D_SPECIAL_ATTRS[8]       = {2104, 2105, 2204, 2205, 2404, 2405, 2406, 2304};
__constant__ int D_TOTAL_ATTR_POWER_VALUES[121] = {
    0, 5, 11, 17, 23, 29, 34, 40, 46, 52, 58, 64, 69, 75, 81, 87, 93, 99, 104, 110, 116,
    122, 128, 133, 139, 145, 151, 157, 163, 168, 174, 180, 186, 192, 198, 203, 209, 215, 221, 227, 233,
    238, 244, 250, 256, 262, 267, 273, 279, 285, 291, 297, 302, 308, 314, 320, 326, 332, 337, 343, 349,
    355, 361, 366, 372, 378, 384, 390, 396, 401, 407, 413, 419, 425, 431, 436, 442, 448, 454, 460, 466,
    471, 477, 483, 489, 495, 500, 506, 512, 518, 524, 530, 535, 541, 547, 553, 559, 565, 570, 576, 582,
    588, 594, 599, 605, 611, 617, 623, 629, 634, 640, 646, 652, 658, 664, 669, 675, 681, 687, 693, 699};

// 属性聚合槽数量上限。游戏目前 21 种属性，10件×4词条最多40次聚合。
// 32 个槽留有余量；若属性种类增加需同步调整此常量。
#define MAX_AGG_SLOTS 32

__global__ void TestKernel(int *data, int size) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < size) data[idx] = idx * 2;
}

__device__ long long GpuCombinationCount(int n, int r) {
    if (r > n || r < 0) return 0;
    if (r == 0 || r == n) return 1;
    if (r > n - r) r = n - r;
    long long result = 1;
    for (int i = 0; i < r; ++i) result = result * (n - i) / (i + 1);
    return result;
}

// 通用组合解包（支持任意 r 件，最大10件）
// 优化：内层用二分搜索代替线性扫描（O(r*log(n)) vs O(r*n)）
__device__ void GpuGetCombinationByIndex(int n, int r, long long index, int *combination) {
    long long remaining = index;
    for (int i = 0; i < r; ++i) {
        int start = (i == 0) ? 0 : combination[i - 1] + 1;
        int end   = n - r + i;   // combination[i] 的最大合法值
        // 二分搜索：找到最小的 j 使得 sum_{x=start..j} C(n-x-1, r-i-1) > remaining
        int lo = start, hi = end;
        while (lo < hi) {
            int mid = (lo + hi) / 2;
            // 计算从 start 到 mid 的累积组合数
            // prefix_sum(start..mid) = C(n-start, r-i) - C(n-mid-1, r-i)
            long long prefix = GpuCombinationCount(n - start, r - i)
                             - GpuCombinationCount(n - mid - 1, r - i);
            if (prefix <= remaining)
                lo = mid + 1;
            else
                hi = mid;
        }
        // lo 即为 combination[i]
        long long consumed = GpuCombinationCount(n - start, r - i)
                           - GpuCombinationCount(n - lo, r - i);
        remaining -= consumed;
        combination[i] = lo;
    }
}

__device__ inline bool GpuNextCombination(int n, int r, int *comb) {
    for (int pos = r - 1; pos >= 0; --pos) {
        int limit = n - r + pos;
        if (comb[pos] < limit) {
            ++comb[pos];
            for (int k = pos + 1; k < r; ++k) comb[k] = comb[k - 1] + 1;
            return true;
        }
    }
    return false;
}

// ─────────────────────────────────────────────────────────────────────────────
//  GpuEnumerationKernel：通用版，支持 combo_size 1~10
//
//  结果打包策略：
//    每个解用 slots_per_solution = ceil(combo_size/4) 个 long long 存索引
//    第 i 个解的第 slot 个 long long 在 indices[i * slots_per_solution + slot]
//    每个 long long 按 16bit 打包，最多存4个索引
// ─────────────────────────────────────────────────────────────────────────────
__global__ void GpuEnumerationKernel(
    const int *__restrict__ attr_ids,
    const int *__restrict__ attr_values,
    const int *__restrict__ attr_counts,
    const int *__restrict__ offsets,
    int module_count,
    long long start_combination,
    long long end_combination,
    const int *__restrict__ target_attrs,
    int target_count,
    const int *__restrict__ exclude_attrs,
    int exclude_count,
    const int *__restrict__ min_attr_ids,
    const int *__restrict__ min_attr_values,
    int min_attr_count,
    int combo_size,
    int slots_per_solution,   // = ceil(combo_size / 4)
    int *scores,
    long long *indices)        // 每解占 slots_per_solution 个 long long
{
    long long tid = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long total_threads = (long long)gridDim.x * blockDim.x;

    long long R = end_combination - start_combination;
    if (R <= 0) return;
    long long L = (R + total_threads - 1) / total_threads;
    long long seg_start = start_combination + tid * L;
    if (seg_start >= end_combination) return;
    long long seg_end = min(seg_start + L, end_combination);

    int combo[10];  // 最大支持10件
    GpuGetCombinationByIndex(module_count, combo_size, seg_start, combo);

    for (long long combo_idx = seg_start; combo_idx < seg_end; ++combo_idx) {
        long long output_idx = combo_idx - start_combination;

        // 属性聚合 - 使用 MAX_AGG_SLOTS 个槽（游戏中最多21种属性，留余量）
        int aggregated_ids[MAX_AGG_SLOTS];
        int aggregated_values[MAX_AGG_SLOTS];
        for (int _i = 0; _i < MAX_AGG_SLOTS; ++_i) { aggregated_ids[_i] = 0; aggregated_values[_i] = 0; }
        int agg_count = 0;
        int total_attr_value = 0;

        for (int m = 0; m < combo_size; ++m) {
            int module_idx = combo[m];
            int start_off  = offsets[module_idx];
            int attr_cnt   = attr_counts[module_idx];
            for (int i = 0; i < attr_cnt; ++i) {
                int attr_id    = attr_ids[start_off + i];
                int attr_value = attr_values[start_off + i];
                total_attr_value += attr_value;
                int found_idx = -1;
                for (int j = 0; j < agg_count && j < MAX_AGG_SLOTS; ++j) {
                    if (aggregated_ids[j] == attr_id) { found_idx = j; break; }
                }
                if (found_idx >= 0) aggregated_values[found_idx] += attr_value;
                else if (agg_count < MAX_AGG_SLOTS) {
                    aggregated_ids[agg_count]   = attr_id;
                    aggregated_values[agg_count] = attr_value;
                    ++agg_count;
                }
            }
        }

        // min_attr_sum 约束
        // Bug修复3: 无效解用 INT_MIN 作哨兵，避免与合法零分混淆，
        //           同时让 Radix 阈值选取时将它们排在最底部（unsigned cast 后仍是 0x80000000，
        //           低于任何有效正分的 unsigned 表示）
        if (min_attr_count > 0) {
            bool valid = true;
            for (int req_idx = 0; req_idx < min_attr_count; ++req_idx) {
                int req_id  = min_attr_ids[req_idx];
                int req_val = min_attr_values[req_idx];
                int actual  = 0;
                for (int j = 0; j < agg_count; ++j) {
                    if (aggregated_ids[j] == req_id) { actual = aggregated_values[j]; break; }
                }
                if (actual < req_val) { valid = false; break; }
            }
            if (!valid) {
                for (int s = 0; s < slots_per_solution; ++s)
                    indices[output_idx * slots_per_solution + s] = 0LL;
                scores[output_idx] = INT_MIN;   // 负哨兵；后续 Radix/Flag/Host 均会跳过 score < 0
                if (!GpuNextCombination(module_count, combo_size, combo)) break;
                continue;
            }
        }

        // 评分
        // Bug修复2: ATTR_THRESHOLDS 单调递增，遇到不满足条件的阈值后更高阈值
        //           必然也不满足，添加 else break 提前退出，与 CPU/OpenCL 行为一致
        int threshold_power = 0;
        for (int i = 0; i < agg_count; ++i) {
            int attr_id = aggregated_ids[i], attr_value = aggregated_values[i];
            int max_level = 0;
            for (int j = 0; j < 6; ++j) {
                if (attr_value >= D_ATTR_THRESHOLDS[j]) max_level = j + 1;
                else break;   // 阈值单调递增，后续不可能满足
            }
            if (max_level > 0) {
                bool is_special = false;
                for (int j = 0; j < 8; ++j) { if (attr_id == D_SPECIAL_ATTRS[j]){is_special=true;break;} }
                int bp = is_special ? D_SPECIAL_POWER_VALUES[max_level-1] : D_BASIC_POWER_VALUES[max_level-1];
                int multiplier = 1;
                for (int j = 0; j < target_count; ++j) { if (attr_id==target_attrs[j]){multiplier=2;break;} }
                if (multiplier != 2) {
                    for (int j = 0; j < exclude_count; ++j) { if (attr_id==exclude_attrs[j]){multiplier=0;break;} }
                }
                threshold_power += bp * multiplier;
            }
        }
        // Bug修复6: 改用与其它后端一致的写法 > 120
        int capped = total_attr_value > 120 ? 120 : total_attr_value;
        scores[output_idx] = threshold_power + D_TOTAL_ATTR_POWER_VALUES[capped];

        // 打包索引：每4个索引放1个 long long
        for (int s = 0; s < slots_per_solution; ++s) {
            long long packed = 0;
            int base = s * 4;
            int in_slot = combo_size - base;
            if (in_slot > 4) in_slot = 4;
            for (int j = 0; j < in_slot; ++j)
                packed |= ((long long)combo[base + j] << (j * 16));
            indices[output_idx * slots_per_solution + s] = packed;
        }

        if (!GpuNextCombination(module_count, combo_size, combo)) break;
    }
}

__global__ void HistogramByteKernel(
    const int *__restrict__ scores, long long n,
    unsigned int prefix_mask, unsigned int prefix_value,
    int byte_idx, unsigned int *__restrict__ g_hist) {
    __shared__ unsigned int s_hist[256];
    for (int i = threadIdx.x; i < 256; i += blockDim.x) s_hist[i] = 0U;
    __syncthreads();
    long long idx = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long stride = (long long)blockDim.x * gridDim.x;
    int shift = byte_idx * 8;
    for (; idx < n; idx += stride) {
        int score = scores[idx];
        // 仅让有效分数参与 Radix 阈值选择；无效解（负分哨兵）必须完全排除，
        // 否则 signed->unsigned 转换后会在字节序上被错误地当成超大值。
        if (score >= 0) {
            unsigned int s = (unsigned int)score;
            if ((s & prefix_mask) == prefix_value)
                atomicAdd(&s_hist[(s >> shift) & 0xFFU], 1U);
        }
    }
    __syncthreads();
    for (int i = threadIdx.x; i < 256; i += blockDim.x) atomicAdd(&g_hist[i], s_hist[i]);
}

static int Radix256SelectThreshold(const int *d_scores, long long n, int k, int grid_size, int block_size) {
    unsigned int *d_hist = nullptr;
    if (cudaMalloc(&d_hist, 256 * sizeof(unsigned int)) != cudaSuccess) return 0;
    unsigned int prefix_mask = 0U, prefix_value = 0U;
    for (int byte_idx = 3; byte_idx >= 0; --byte_idx) {
        cudaMemset(d_hist, 0, 256 * sizeof(unsigned int));
        HistogramByteKernel<<<grid_size, block_size>>>(d_scores, n, prefix_mask, prefix_value, byte_idx, d_hist);
        if (cudaGetLastError() != cudaSuccess || cudaDeviceSynchronize() != cudaSuccess) {
            cudaFree(d_hist); return 0;
        }
        unsigned int h_hist[256];
        if (cudaMemcpy(h_hist, d_hist, 256 * sizeof(unsigned int), cudaMemcpyDeviceToHost) != cudaSuccess) {
            cudaFree(d_hist); return 0;
        }
        unsigned int acc = 0U; int chosen_bucket = 0;
        for (int b = 255; b >= 0; --b) {
            acc += h_hist[b];
            if (acc >= (unsigned int)k) { chosen_bucket = b; break; }
        }
        unsigned int bigger_acc = acc - h_hist[chosen_bucket];
        k -= (int)bigger_acc;
        prefix_mask  |= (0xFFU << (byte_idx * 8));
        prefix_value |= ((unsigned int)chosen_bucket << (byte_idx * 8));
    }
    cudaFree(d_hist);
    return (int)prefix_value;
}

__global__ void SetFlagsGeKernel(const int *__restrict__ scores, long long n, int threshold, unsigned char *__restrict__ flags) {
    long long idx = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long stride = (long long)blockDim.x * gridDim.x;
    for (; idx < n; idx += stride) {
        int score = scores[idx];
        flags[idx] = (score >= 0 && score >= threshold) ? 1 : 0;
    }
}

// CompactSelectedKernel: 将满足 flag 条件的 (score, indices) 原子地成对写入紧凑输出缓冲区
// 这是正确性的关键：score 与 indices 始终以原子方式同步写入同一位置，不存在错位风险
// 与 OpenCL 的 compact_selected kernel 逻辑完全一致
__global__ void CompactSelectedKernel(
    const int        *__restrict__ scores,
    const long long  *__restrict__ indices,
    const unsigned char *__restrict__ flags,
    long long n,
    int        *__restrict__ out_scores,
    long long  *__restrict__ out_indices,
    int        *__restrict__ out_count,
    int compact_cap,             // 输出缓冲区最大容量
    int slots_per_solution) {
    long long idx    = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long stride = (long long)gridDim.x  * blockDim.x;
    for (; idx < n; idx += stride) {
        if (flags[idx]) {
            int pos = atomicAdd(out_count, 1);
            if (pos < compact_cap) {
                out_scores[pos] = scores[idx];
                for (int s = 0; s < slots_per_solution; ++s)
                    out_indices[(long long)pos * slots_per_solution + s] =
                        indices[idx * slots_per_solution + s];
            }
        }
    }
}

int GetGpuConfig(GpuConfig *config) {
    cudaDeviceProp prop;
    if (cudaGetDeviceProperties(&prop, 0) != cudaSuccess) return 0;
    config->max_threads_per_block    = prop.maxThreadsPerBlock;
    config->max_blocks_per_sm        = prop.maxBlocksPerMultiProcessor;
    config->multiprocessor_count     = prop.multiProcessorCount;
    config->max_grid_size            = prop.maxGridSize[0];
    config->global_memory            = prop.totalGlobalMem;
    config->compute_capability_major = prop.major;
    config->compute_capability_minor = prop.minor;
    return 1;
}

void CalculateOptimalParams(GpuConfig *config, long long total_combinations, int slots_per_solution, int compact_cap) {
    config->optimal_block_size = min(512, config->max_threads_per_block);
    int total_cores = config->multiprocessor_count * config->max_blocks_per_sm;
    config->optimal_grid_size = min(total_cores * 2, config->max_grid_size);
    long long max_ct = (long long)config->optimal_grid_size * config->optimal_block_size;
    if (total_combinations < max_ct)
        config->optimal_grid_size = (int)((total_combinations + config->optimal_block_size - 1) / config->optimal_block_size);

    // Bug修复5: 内存估算同时计入 compact 缓冲区开销
    //   batch 缓冲区：scores(int) + indices(slots*ll) + flags(uchar)
    //   compact 缓冲区：comp_scores(int) + comp_indices(slots*ll)  — 固定额外开销
    size_t available_memory = (size_t)((double)config->global_memory * 0.4);
    size_t compact_overhead = (size_t)compact_cap *
        (sizeof(int) + (size_t)slots_per_solution * sizeof(long long));
    if (available_memory > compact_overhead)
        available_memory -= compact_overhead;
    else
        available_memory = 1;   // 防止下溢，后续会被 100000 下界覆盖

    size_t bytes_per_entry = sizeof(int)
        + (size_t)slots_per_solution * sizeof(long long)
        + sizeof(unsigned char);
    long long memory_limited_batch = (long long)(available_memory / bytes_per_entry);
    long long compute_limited_batch = max_ct * 3000;
    config->optimal_batch_size = max(100000LL, min(memory_limited_batch, compute_limited_batch));
    config->optimal_batch_size = min(config->optimal_batch_size, 22500000LL);
}

extern "C" int TestCuda() {
    int device_count = 0;
    if (cudaGetDeviceCount(&device_count) != cudaSuccess || device_count == 0) return 0;
    int *d_data;
    if (cudaMalloc(&d_data, 1024 * sizeof(int)) != cudaSuccess) return 0;
    TestKernel<<<4, 256>>>(d_data, 1024);
    cudaError_t err = cudaDeviceSynchronize();
    cudaFree(d_data);
    return (err == cudaSuccess) ? 1 : 0;
}

long long CpuCombinationCount(int n, int r) {
    if (r > n || r < 0) return 0;
    if (r == 0 || r == n) return 1;
    if (r > n - r) r = n - r;
    long long result = 1;
    for (int i = 0; i < r; ++i) result = result * (n - i) / (i + 1);
    return result;
}

// ─────────────────────────────────────────────────────────────────────────────
//  GpuStrategyEnumeration：完整CUDA枚举，支持 combo_size 1~10
//
//  result_indices 内存布局：
//    每解占 slots_per_solution = ceil(combo_size/4) 个 long long
//    调用方需分配 max_solutions * slots_per_solution 个 long long
// ─────────────────────────────────────────────────────────────────────────────
extern "C" int GpuStrategyEnumeration(
    const int *module_attr_ids,
    const int *module_attr_values,
    const int *module_attr_counts,
    const int *module_offsets,
    int module_count,
    int total_attrs,
    const int *target_attrs,
    int target_count,
    const int *exclude_attrs,
    int exclude_count,
    const int *min_attr_ids,
    const int *min_attr_values,
    int min_attr_count,
    int combo_size,
    int max_solutions,
    int *result_scores,
    long long *result_indices)
{
    long long total_combinations = CpuCombinationCount(module_count, combo_size);
    int slots_per_solution = (combo_size + 3) / 4;
    // combo_size 最大支持 10 件，slots_per_solution ≤ 3；超出则拒绝
    if (slots_per_solution > 3) {
        printf("ERROR: combo_size=%d exceeds maximum supported value of 10 (slots_per_solution=%d > 3)\n",
               combo_size, slots_per_solution);
        return 0;
    }

    GpuConfig gpu_config;
    if (!GetGpuConfig(&gpu_config)) { printf("Failed to get GPU configuration\n"); return 0; }

    // Bug修复5: compact_cap 先算出来，再传给 CalculateOptimalParams 以精确估算内存
    // compact_cap 是 Radix Top-K 紧凑输出的最大条目数
    // 预先用临时值计算，后续与 batch_size 再对齐
    int compact_cap_prelim = max_solutions * 8;  // 初步估算，与后面对齐

    CalculateOptimalParams(&gpu_config, total_combinations, slots_per_solution, compact_cap_prelim);

    printf("GPU Configuration: CC=%d.%d, SM=%d, Mem=%.1fMB\n",
        gpu_config.compute_capability_major, gpu_config.compute_capability_minor,
        gpu_config.multiprocessor_count, (double)gpu_config.global_memory/(1024*1024));
    printf("combo_size=%d, slots_per_solution=%d, total_combinations=%lld\n",
        combo_size, slots_per_solution, total_combinations);

    long long batch_size = gpu_config.optimal_batch_size;

    // ── 紧凑输出缓冲区大小：Radix Top-K 阈值确保被 flag 的元素约为 max_solutions 个，
    //    乘以 8 以应对 score 相等时的 tie 情况，上限 batch_size ──
    int compact_cap = (int)std::min((long long)(max_solutions * 8), batch_size);

    // Bug修复4: 不再将 idx_slots 硬编码为 [3]，改为 [MAX_SLOTS] 并在运行时以
    //           slots_per_solution 控制实际读写范围，防止 combo_size 增大时越界
    // MAX_SLOTS = ceil(10/4) = 3，与 combo_size 最大值 10 一致
    constexpr int MAX_SLOTS = 3;
    struct SolItem {
        int score;
        long long idx_slots[MAX_SLOTS];  // 有效范围 [0, slots_per_solution)
        bool operator>(const SolItem& o) const { return score > o.score; }
    };
    // min-heap，size ≤ max_solutions
    auto cmp = [](const SolItem& a, const SolItem& b){ return a.score > b.score; };
    std::priority_queue<SolItem, std::vector<SolItem>, decltype(cmp)> topk(cmp);

    // 提前声明所有变量，避免 goto 跳过初始化（warning #546）
    int *d_attr_ids=nullptr, *d_attr_values=nullptr, *d_attr_counts=nullptr, *d_offsets=nullptr;
    int *d_target_attrs=nullptr, *d_exclude_attrs=nullptr;
    int *d_min_attr_ids=nullptr, *d_min_attr_values=nullptr;
    int *d_scores=nullptr;
    long long *d_indices=nullptr;
    unsigned char *d_flags=nullptr;
    int *d_out_count=nullptr;
    int *d_comp_scores=nullptr;
    long long *d_comp_indices=nullptr;
    cudaError_t err = cudaSuccess;

#define CUDA_CHECK(call, msg) do { err=(call); if(err!=cudaSuccess){printf("ERROR: %s: %s\n",(msg),cudaGetErrorString(err));goto cleanup;} } while(0)

    CUDA_CHECK(cudaMalloc(&d_attr_ids,    total_attrs   * sizeof(int)), "malloc attr_ids");
    CUDA_CHECK(cudaMalloc(&d_attr_values, total_attrs   * sizeof(int)), "malloc attr_values");
    CUDA_CHECK(cudaMalloc(&d_attr_counts, module_count  * sizeof(int)), "malloc attr_counts");
    CUDA_CHECK(cudaMalloc(&d_offsets,     module_count  * sizeof(int)), "malloc offsets");
    if (target_count > 0) {
        CUDA_CHECK(cudaMalloc(&d_target_attrs, target_count * sizeof(int)), "malloc target_attrs");
        CUDA_CHECK(cudaMemcpy(d_target_attrs, target_attrs, target_count*sizeof(int), cudaMemcpyHostToDevice), "memcpy target_attrs");
    }
    if (exclude_count > 0) {
        CUDA_CHECK(cudaMalloc(&d_exclude_attrs, exclude_count * sizeof(int)), "malloc exclude_attrs");
        CUDA_CHECK(cudaMemcpy(d_exclude_attrs, exclude_attrs, exclude_count*sizeof(int), cudaMemcpyHostToDevice), "memcpy exclude_attrs");
    }
    if (min_attr_count > 0) {
        CUDA_CHECK(cudaMalloc(&d_min_attr_ids,    min_attr_count * sizeof(int)), "malloc min_attr_ids");
        CUDA_CHECK(cudaMemcpy(d_min_attr_ids,    min_attr_ids,    min_attr_count*sizeof(int), cudaMemcpyHostToDevice), "memcpy min_attr_ids");
        CUDA_CHECK(cudaMalloc(&d_min_attr_values, min_attr_count * sizeof(int)), "malloc min_attr_values");
        CUDA_CHECK(cudaMemcpy(d_min_attr_values, min_attr_values, min_attr_count*sizeof(int), cudaMemcpyHostToDevice), "memcpy min_attr_values");
    }
    CUDA_CHECK(cudaMalloc(&d_scores,       batch_size * sizeof(int)), "malloc scores");
    CUDA_CHECK(cudaMalloc(&d_indices,      (size_t)batch_size * slots_per_solution * sizeof(long long)), "malloc indices");
    CUDA_CHECK(cudaMalloc(&d_flags,        batch_size * sizeof(unsigned char)), "malloc flags");
    CUDA_CHECK(cudaMalloc(&d_out_count,    sizeof(int)), "malloc out_count");
    CUDA_CHECK(cudaMalloc(&d_comp_scores,  compact_cap * sizeof(int)), "malloc comp_scores");
    CUDA_CHECK(cudaMalloc(&d_comp_indices, (size_t)compact_cap * slots_per_solution * sizeof(long long)), "malloc comp_indices");

    CUDA_CHECK(cudaMemcpy(d_attr_ids,    module_attr_ids,    total_attrs  *sizeof(int), cudaMemcpyHostToDevice), "memcpy attr_ids");
    CUDA_CHECK(cudaMemcpy(d_attr_values, module_attr_values, total_attrs  *sizeof(int), cudaMemcpyHostToDevice), "memcpy attr_values");
    CUDA_CHECK(cudaMemcpy(d_attr_counts, module_attr_counts, module_count *sizeof(int), cudaMemcpyHostToDevice), "memcpy attr_counts");
    CUDA_CHECK(cudaMemcpy(d_offsets,     module_offsets,     module_count *sizeof(int), cudaMemcpyHostToDevice), "memcpy offsets");

    // ── 批次处理：Radix阈值 → Flag → CompactSelected → HostPriorityQueue ──
    // 与 OpenCL 实现逻辑完全对应，保证 score 与 indices 永远原子绑定
    for (long long batch_start = 0; batch_start < total_combinations; batch_start += batch_size) {
        long long cur_batch = min(batch_size, total_combinations - batch_start);

        // 1. 枚举核心 kernel
        {
            dim3 block(gpu_config.optimal_block_size);
            int gs = min(gpu_config.optimal_grid_size, (int)((cur_batch + block.x - 1) / block.x));
            dim3 grid(gs);
            GpuEnumerationKernel<<<grid, block>>>(
                d_attr_ids, d_attr_values, d_attr_counts, d_offsets, module_count,
                batch_start, batch_start + cur_batch,
                d_target_attrs, target_count, d_exclude_attrs, exclude_count,
                d_min_attr_ids, d_min_attr_values, min_attr_count,
                combo_size, slots_per_solution,
                d_scores, d_indices);
            CUDA_CHECK(cudaGetLastError(), "kernel launch");
            CUDA_CHECK(cudaDeviceSynchronize(), "kernel sync");
        }

        // 2. Radix Top-K 阈值选取（O(N)）
        int grid_sel = min(gpu_config.optimal_grid_size, (int)((cur_batch + gpu_config.optimal_block_size - 1) / gpu_config.optimal_block_size));
        int threshold = Radix256SelectThreshold(d_scores, cur_batch, max_solutions, grid_sel, gpu_config.optimal_block_size);

        // 3. Flag：标记得分 ≥ threshold 的元素
        SetFlagsGeKernel<<<grid_sel, gpu_config.optimal_block_size>>>(d_scores, cur_batch, threshold, d_flags);
        CUDA_CHECK(cudaGetLastError(), "flag kernel");
        CUDA_CHECK(cudaDeviceSynchronize(), "flag sync");

        // 4. 原子紧凑：将 (score, indices) 成对写入紧凑缓冲区
        int zero = 0;
        CUDA_CHECK(cudaMemcpy(d_out_count, &zero, sizeof(int), cudaMemcpyHostToDevice), "reset out_count");
        CompactSelectedKernel<<<grid_sel, gpu_config.optimal_block_size>>>(
            d_scores, d_indices, d_flags, cur_batch,
            d_comp_scores, d_comp_indices, d_out_count, compact_cap, slots_per_solution);
        CUDA_CHECK(cudaGetLastError(), "compact kernel");
        CUDA_CHECK(cudaDeviceSynchronize(), "compact sync");

        // 5. 读回紧凑结果
        int h_count = 0;
        CUDA_CHECK(cudaMemcpy(&h_count, d_out_count, sizeof(int), cudaMemcpyDeviceToHost), "copy out_count");
        h_count = std::min(h_count, compact_cap);

        if (h_count > 0) {
            std::vector<int>       h_scores(h_count);
            std::vector<long long> h_indices((size_t)h_count * slots_per_solution);
            CUDA_CHECK(cudaMemcpy(h_scores.data(), d_comp_scores, h_count * sizeof(int), cudaMemcpyDeviceToHost), "copy comp_scores");
            CUDA_CHECK(cudaMemcpy(h_indices.data(), d_comp_indices, (size_t)h_count * slots_per_solution * sizeof(long long), cudaMemcpyDeviceToHost), "copy comp_indices");

            // 负分哨兵只用于后续主机端过滤；Radix/Flag 阶段也会显式跳过 score < 0，
            // 这样既不会误杀合法 0 分组合，也不会让无效解污染阈值选择。
            for (int i = 0; i < h_count; ++i) {
                int sc = h_scores[i];
                if (sc < 0) continue;   // 跳过 INT_MIN 哨兵（min_attr 不满足的无效解）
                SolItem item;
                item.score = sc;
                for (int s = 0; s < slots_per_solution && s < 3; ++s)
                    item.idx_slots[s] = h_indices[(size_t)i * slots_per_solution + s];
                if ((int)topk.size() < max_solutions) {
                    topk.push(item);
                } else if (sc > topk.top().score) {
                    topk.pop();
                    topk.push(item);
                }
            }
        }
    }

    // ── 从最小堆取出结果（升序出堆，逆序填充 → 降序输出）──
    {
        int out_n = (int)topk.size();
        std::vector<SolItem> items;
        items.reserve(out_n);
        while (!topk.empty()) { items.push_back(topk.top()); topk.pop(); }
        // 堆弹出为升序，逆转得降序
        std::reverse(items.begin(), items.end());
        for (int i = 0; i < out_n; ++i) {
            result_scores[i] = items[i].score;
            for (int s = 0; s < slots_per_solution && s < 3; ++s)
                result_indices[(size_t)i * slots_per_solution + s] = items[i].idx_slots[s];
        }
        // 用0填充未使用的尾部
        for (int i = out_n; i < max_solutions; ++i) {
            result_scores[i] = 0;
            for (int s = 0; s < slots_per_solution; ++s)
                result_indices[(size_t)i * slots_per_solution + s] = 0LL;
        }
        // 返回实际结果数（与 OpenCL 一致，非固定 max_solutions）
        int final_count = out_n;

        // 统一清理所有 GPU 缓冲区（消除重复代码）
        auto free_all = [&]() {
            if (d_attr_ids)       cudaFree(d_attr_ids);
            if (d_attr_values)    cudaFree(d_attr_values);
            if (d_attr_counts)    cudaFree(d_attr_counts);
            if (d_offsets)        cudaFree(d_offsets);
            if (d_target_attrs)   cudaFree(d_target_attrs);
            if (d_exclude_attrs)  cudaFree(d_exclude_attrs);
            if (d_min_attr_ids)   cudaFree(d_min_attr_ids);
            if (d_min_attr_values)cudaFree(d_min_attr_values);
            if (d_scores)         cudaFree(d_scores);
            if (d_indices)        cudaFree(d_indices);
            if (d_flags)          cudaFree(d_flags);
            if (d_out_count)      cudaFree(d_out_count);
            if (d_comp_scores)    cudaFree(d_comp_scores);
            if (d_comp_indices)   cudaFree(d_comp_indices);
        };
        free_all();
        return final_count;
    }

cleanup:
    {
        auto free_all = [&]() {
            if (d_attr_ids)       cudaFree(d_attr_ids);
            if (d_attr_values)    cudaFree(d_attr_values);
            if (d_attr_counts)    cudaFree(d_attr_counts);
            if (d_offsets)        cudaFree(d_offsets);
            if (d_target_attrs)   cudaFree(d_target_attrs);
            if (d_exclude_attrs)  cudaFree(d_exclude_attrs);
            if (d_min_attr_ids)   cudaFree(d_min_attr_ids);
            if (d_min_attr_values)cudaFree(d_min_attr_values);
            if (d_scores)         cudaFree(d_scores);
            if (d_indices)        cudaFree(d_indices);
            if (d_flags)          cudaFree(d_flags);
            if (d_out_count)      cudaFree(d_out_count);
            if (d_comp_scores)    cudaFree(d_comp_scores);
            if (d_comp_indices)   cudaFree(d_comp_indices);
        };
        free_all();
    }
    return 0;
}
