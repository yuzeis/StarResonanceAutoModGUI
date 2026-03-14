#include "module_optimizer.h"

#ifdef USE_CUDA
extern "C" int TestCuda();
extern "C" int GpuStrategyEnumeration(
    const int* module_attr_ids,
    const int* module_attr_values,
    const int* module_attr_counts,
    const int* module_offsets,
    int module_count,
    int total_attrs,
    const int* target_attrs,
    int target_count,
    const int* exclude_attrs,
    int exclude_count,
    const int* min_attr_ids,
    const int* min_attr_values,
    int min_attr_count,
    int combo_size,
    int max_solutions,
    int* result_scores,
    long long* result_indices);
#endif

// 通用 NextCombination，支持 1~10 任意件数
inline bool NextCombination(std::vector<uint16_t>& comb, size_t n, size_t r) {
    for (int pos = static_cast<int>(r) - 1; pos >= 0; --pos) {
        uint16_t limit = static_cast<uint16_t>(n - r + pos);
        if (comb[pos] < limit) {
            ++comb[pos];
            for (size_t k = pos + 1; k < r; ++k)
                comb[k] = static_cast<uint16_t>(comb[k - 1] + 1);
            return true;
        }
    }
    return false;
}

size_t CombinationCount(size_t n, size_t r) {
    if (r > n) return 0;
    if (r == 0 || r == n) return 1;
    if (r > n - r) r = n - r;
    size_t result = 1;
    for (size_t i = 0; i < r; ++i) result = result * (n - i) / (i + 1);
    return result;
}

void GetCombinationByIndex(size_t n, size_t r, size_t index, std::vector<size_t>& combination) {
    size_t remaining = index;
    for (size_t i = 0; i < r; ++i) {
        size_t start = (i == 0) ? 0 : combination[i-1] + 1;
        size_t end   = n - r + i;  // 最大合法值
        // 二分搜索：找到最小的 j 使得 prefix_sum(start..j) > remaining
        size_t lo = start, hi = end;
        while (lo < hi) {
            size_t mid = lo + (hi - lo) / 2;
            // prefix_sum(start..mid) = C(n-start, r-i) - C(n-mid-1, r-i)
            size_t prefix = CombinationCount(n - start, r - i)
                          - CombinationCount(n - mid - 1, r - i);
            if (prefix <= remaining)
                lo = mid + 1;
            else
                hi = mid;
        }
        size_t consumed = CombinationCount(n - start, r - i)
                        - CombinationCount(n - lo, r - i);
        remaining -= consumed;
        combination[i] = lo;
    }
}

// ProcessCombinationRange: 通用版支持 combo_size 1~10
std::vector<CompactSolution> ModuleOptimizerCpp::ProcessCombinationRange(
    size_t start_combination, size_t end_combination, size_t n,
    const std::vector<ModuleInfo>& modules,
    const std::unordered_set<int>& target_attributes,
    const std::unordered_set<int>& exclude_attributes,
    const std::unordered_map<int, int>& min_attr_sum_requirements,
    int local_top_capacity,
    int combo_size) {

    size_t r = static_cast<size_t>(combo_size);
    size_t range_size = end_combination - start_combination;

    std::vector<CompactSolution> solution;
    // ext_space 控制 nth_element 裁剪前允许的超额条目数
    // 过大会导致内存峰值翻倍；使用 min(cap/2, 2048) 平衡裁剪频率和内存
    int ext_space = std::min(std::max(local_top_capacity / 2, 64), 2048);
    solution.reserve(std::min(range_size, static_cast<size_t>(local_top_capacity + ext_space)));
    int current_min = std::numeric_limits<int>::min();

    std::vector<uint16_t> cb(r);
    std::vector<size_t> tmp(r);
    GetCombinationByIndex(n, r, start_combination, tmp);
    for (size_t j = 0; j < r; ++j) cb[j] = static_cast<uint16_t>(tmp[j]);

    // 评分 lambda
    auto score_combo = [&]() -> int {
        int attr_ids[80] = {}, attr_vals[80] = {};
        int attr_cnt = 0, total_attr_value = 0;
        for (size_t m = 0; m < r; ++m) {
            for (const auto& p : modules[static_cast<size_t>(cb[m])].parts) {
                bool found = false;
                for (int i = 0; i < attr_cnt; ++i) { if (attr_ids[i]==p.id){attr_vals[i]+=p.value;found=true;break;} }
                if (!found && attr_cnt < 80) { attr_ids[attr_cnt]=p.id; attr_vals[attr_cnt]=p.value; ++attr_cnt; }
                total_attr_value += p.value;
            }
        }
        int threshold_power = 0;
        for (int i = 0; i < attr_cnt; ++i) {
            int av = attr_vals[i], aid = attr_ids[i];
            int max_level = 0;
            for (int lv = 0; lv < 6; ++lv) { if (av >= Constants::ATTR_THRESHOLDS[lv]) max_level = lv+1; else break; }
            if (max_level > 0) {
                bool is_sp = Constants::SPECIAL_ATTR_NAMES.count(aid) > 0;
                int bp = is_sp ? Constants::SPECIAL_ATTR_POWER_VALUES[max_level-1]
                               : Constants::BASIC_ATTR_POWER_VALUES[max_level-1];
                if (!target_attributes.empty() && target_attributes.count(aid))
                    threshold_power += bp * 2;
                else if (!exclude_attributes.empty() && exclude_attributes.count(aid))
                    threshold_power += 0;
                else
                    threshold_power += bp;
            }
        }
        int capped = std::min(total_attr_value, 120);
        return threshold_power + Constants::TOTAL_ATTR_POWER_VALUES[capped];
    };

    // min_attr_sum 检查
    auto check_min = [&]() -> bool {
        if (min_attr_sum_requirements.empty()) return true;
        int sums[80] = {}, ids[80] = {};
        int cnt = 0;
        for (size_t m = 0; m < r; ++m) {
            for (const auto& p : modules[static_cast<size_t>(cb[m])].parts) {
                bool f = false;
                for (int i = 0; i < cnt; ++i) { if (ids[i]==p.id){sums[i]+=p.value;f=true;break;} }
                if (!f && cnt < 80) { ids[cnt]=p.id; sums[cnt]=p.value; ++cnt; }
            }
        }
        for (const auto& kv : min_attr_sum_requirements) {
            int got = 0;
            for (int i = 0; i < cnt; ++i) { if (ids[i]==kv.first){got=sums[i];break;} }
            if (got < kv.second) return false;
        }
        return true;
    };

    // 打包索引：前4个放 packed_indices，第5~10个放 extra_indices
    auto make_cs = [&](int score) -> CompactSolution {
        CompactSolution cs;
        cs.score = score;
        cs.combo_size = static_cast<int>(r);
        cs.packed_indices = 0;
        size_t pack_n = std::min(r, static_cast<size_t>(4));
        for (size_t i = 0; i < pack_n; ++i)
            cs.packed_indices |= (static_cast<uint64_t>(cb[i]) << (i * 16));
        cs.extra_indices.clear();
        for (size_t i = 4; i < r; ++i)
            cs.extra_indices.push_back(cb[i]);
        return cs;
    };

    size_t produced = 0;
    while (produced < range_size) {
        if (check_min()) {
            int score = score_combo();
            if (static_cast<int>(solution.size()) < local_top_capacity) {
                solution.emplace_back(make_cs(score));
                if (static_cast<int>(solution.size()) == local_top_capacity) {
                    int mn = solution[0].score;
                    for (int i = 1; i < local_top_capacity; ++i) mn = std::min(mn, solution[i].score);
                    current_min = mn;
                }
            } else if (score > current_min) {
                solution.emplace_back(make_cs(score));
                if (static_cast<int>(solution.size()) == local_top_capacity + ext_space) {
                    std::nth_element(solution.begin(), solution.begin() + local_top_capacity, solution.end(),
                        [](const CompactSolution& a, const CompactSolution& b){ return a.score > b.score; });
                    solution.resize(local_top_capacity);
                    int mn = solution[0].score;
                    for (int i = 1; i < local_top_capacity; ++i) mn = std::min(mn, solution[i].score);
                    current_min = mn;
                }
            }
        }
        ++produced;
        if (produced >= range_size) break;
        if (!NextCombination(cb, n, r)) break;
    }

    if (static_cast<int>(solution.size()) > local_top_capacity) {
        std::nth_element(solution.begin(), solution.begin() + local_top_capacity, solution.end(),
            [](const CompactSolution& a, const CompactSolution& b){ return a.score > b.score; });
        solution.resize(local_top_capacity);
    }
    return solution;
}

std::pair<int, std::map<std::string, int>> ModuleOptimizerCpp::CalculateCombatPower(
    const std::vector<ModuleInfo>& modules) {
    std::unordered_map<std::string, int> attr_breakdown;
    attr_breakdown.reserve(20);
    for (const auto& module : modules)
        for (const auto& part : module.parts)
            attr_breakdown[part.name] += part.value;

    int threshold_power = 0, total_attr_value = 0;
    for (const auto& [attr_name, attr_value] : attr_breakdown) {
        total_attr_value += attr_value;
        int max_level = 0;
        for (int lv = 0; lv < 6; ++lv) { if (attr_value >= Constants::ATTR_THRESHOLDS[lv]) max_level=lv+1; else break; }
        if (max_level > 0) {
            bool is_sp = Constants::SPECIAL_ATTR_NAMES_STR.count(attr_name) > 0;
            threshold_power += is_sp ? Constants::SPECIAL_ATTR_POWER_VALUES[max_level-1]
                                     : Constants::BASIC_ATTR_POWER_VALUES[max_level-1];
        }
    }
    int capped = std::min(total_attr_value, 120);
    std::map<std::string, int> result_map(attr_breakdown.begin(), attr_breakdown.end());
    return {threshold_power + Constants::TOTAL_ATTR_POWER_VALUES[capped], result_map};
}

int ModuleOptimizerCpp::CalculateCombatPowerByIndices(
    const std::vector<size_t>& indices,
    const std::vector<ModuleInfo>& modules,
    const std::unordered_set<int>& target_attributes,
    const std::unordered_set<int>& exclude_attributes) {

    int attr_values[80]={}, attr_ids[80]={};
    int attr_count=0, total_attr_value=0;
    for (size_t index : indices) {
        for (const auto& part : modules[index].parts) {
            bool found = false;
            for (int i=0;i<attr_count;++i){if(attr_ids[i]==part.id){attr_values[i]+=part.value;found=true;break;}}
            if (!found && attr_count<80){attr_ids[attr_count]=part.id;attr_values[attr_count]=part.value;++attr_count;}
            total_attr_value += part.value;
        }
    }
    int threshold_power = 0;
    for (int i=0;i<attr_count;++i){
        int av=attr_values[i],aid=attr_ids[i];
        int max_level=0;
        for(int lv=0;lv<6;++lv){if(av>=Constants::ATTR_THRESHOLDS[lv])max_level=lv+1;else break;}
        if(max_level>0){
            bool is_sp=Constants::SPECIAL_ATTR_NAMES.count(aid)>0;
            int bp=is_sp?Constants::SPECIAL_ATTR_POWER_VALUES[max_level-1]:Constants::BASIC_ATTR_POWER_VALUES[max_level-1];
            if(!target_attributes.empty()&&target_attributes.count(aid)) threshold_power+=bp*2;
            else if(!exclude_attributes.empty()&&exclude_attributes.count(aid)) threshold_power+=0;
            else threshold_power+=bp;
        }
    }
    int capped = std::min(total_attr_value, 120);
    return threshold_power + Constants::TOTAL_ATTR_POWER_VALUES[capped];
}

int ModuleOptimizerCpp::CalculateCombatPowerByPackedIndices(
    uint64_t packed_indices,
    const std::vector<ModuleInfo>& modules,
    const std::unordered_set<int>& target_attributes,
    const std::unordered_set<int>& exclude_attributes,
    int combo_size) {
    std::vector<size_t> idx_vec;
    size_t pack_n = static_cast<size_t>(std::min(combo_size, 4));
    for (size_t i = 0; i < pack_n; ++i)
        idx_vec.push_back(static_cast<size_t>((packed_indices>>(i*16))&0xFFFF));
    return CalculateCombatPowerByIndices(idx_vec, modules, target_attributes, exclude_attributes);
}

// StrategyEnumeration: CPU多线程，支持1~10件
std::vector<ModuleSolution> ModuleOptimizerCpp::StrategyEnumeration(
    const std::vector<ModuleInfo>& modules,
    const std::unordered_set<int>& target_attributes,
    const std::unordered_set<int>& exclude_attributes,
    const std::unordered_map<int, int>& min_attr_sum_requirements,
    int max_solutions,
    int max_workers,
    int combo_size) {

    size_t r = static_cast<size_t>(combo_size);
    std::vector<ModuleInfo> candidate_modules = modules;
    size_t n = candidate_modules.size();
    size_t total_combinations = CombinationCount(n, r);

    // CPU 批次上限：限制单个任务的组合数以保证线程池负载均衡
    // 经验值：约 130 万组合/批次在现代 CPU 上约 50~200ms，避免长尾任务
    static constexpr size_t MAX_BATCH_SIZE = 1'300'000;
    size_t batch_size = std::max(static_cast<size_t>(1000), total_combinations / (max_workers * 4));
    batch_size = std::min(batch_size, MAX_BATCH_SIZE);
    size_t num_batches = (total_combinations + batch_size - 1) / batch_size;

    auto pool = std::make_unique<SimpleThreadPool>(max_workers);
    std::vector<std::future<std::vector<CompactSolution>>> futures;
    futures.reserve(num_batches);

    for (size_t batch_idx = 0; batch_idx < num_batches; ++batch_idx) {
        size_t start_comb = batch_idx * batch_size;
        size_t end_comb = std::min(start_comb + batch_size, total_combinations);
        size_t range_size = end_comb - start_comb;
        int local_cap = static_cast<int>(std::min(range_size, static_cast<size_t>(max_solutions * 2)));
        auto min_req_copy = min_attr_sum_requirements;
        int cs = combo_size;
        futures.push_back(pool->enqueue(
            [start_comb, end_comb, n, local_cap,
             &candidate_modules, target_attributes, exclude_attributes, min_req_copy, cs]() {
                return ProcessCombinationRange(start_comb, end_comb, n,
                    candidate_modules, target_attributes, exclude_attributes, min_req_copy, local_cap, cs);
            }
        ));
    }

    std::priority_queue<CompactSolution, std::vector<CompactSolution>, std::greater<CompactSolution>> top;
    while (!futures.empty()) {
        auto it = std::find_if(futures.begin(), futures.end(),
            [](auto& f){ return f.wait_for(std::chrono::seconds(0)) == std::future_status::ready; });
        if (it != futures.end()) {
            for (const auto& s : it->get()) {
                if (top.size() < static_cast<size_t>(max_solutions)) top.push(s);
                else if (s.score > top.top().score) { top.pop(); top.push(s); }
            }
            futures.erase(it);
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
    pool.reset();

    std::vector<CompactSolution> all;
    all.reserve(top.size());
    while (!top.empty()) { all.push_back(top.top()); top.pop(); }
    std::reverse(all.begin(), all.end());

    std::vector<ModuleSolution> final_solutions;
    final_solutions.reserve(all.size());
    for (const auto& cs_sol : all) {
        std::vector<ModuleInfo> sol_mods;
        size_t pack_n = std::min(static_cast<size_t>(combo_size), static_cast<size_t>(4));
        for (size_t i = 0; i < pack_n; ++i)
            sol_mods.push_back(candidate_modules[(cs_sol.packed_indices >> (i*16)) & 0xFFFF]);
        for (uint16_t ei : cs_sol.extra_indices)
            sol_mods.push_back(candidate_modules[static_cast<size_t>(ei)]);
        auto result = CalculateCombatPower(sol_mods);
        final_solutions.emplace_back(sol_mods, cs_sol.score, result.second);
    }
    return final_solutions;
}

// StrategyEnumerationCUDA: 支持1~10件
std::vector<ModuleSolution> ModuleOptimizerCpp::StrategyEnumerationCUDA(
    const std::vector<ModuleInfo>& modules,
    const std::unordered_set<int>& target_attributes,
    const std::unordered_set<int>& exclude_attributes,
    const std::unordered_map<int, int>& min_attr_sum_requirements,
    int max_solutions,
    int max_workers,
    int combo_size) {
#ifdef USE_CUDA
    if (TestCuda()) {
        printf("CUDA GPU acceleration enabled (combo_size=%d)\n", combo_size);
        std::vector<int> all_attr_ids, all_attr_values, module_attr_counts, module_offsets;
        size_t current_offset = 0;
        for (const auto& module : modules) {
            module_offsets.push_back(static_cast<int>(current_offset));
            module_attr_counts.push_back(static_cast<int>(module.parts.size()));
            for (const auto& part : module.parts) {
                all_attr_ids.push_back(part.id);
                all_attr_values.push_back(part.value);
            }
            current_offset += module.parts.size();
        }
        std::vector<int> target_attrs(target_attributes.begin(), target_attributes.end());
        std::vector<int> exclude_attrs(exclude_attributes.begin(), exclude_attributes.end());
        std::vector<int> min_attr_ids, min_attr_values;
        for (const auto& kv : min_attr_sum_requirements) {
            min_attr_ids.push_back(kv.first); min_attr_values.push_back(kv.second);
        }
        // 分配结果数组：每解需 ceil(combo_size/4) 个 long long 存索引
        int slots_per_solution = (combo_size + 3) / 4;
        std::vector<int> gpu_scores(max_solutions);
        std::vector<long long> gpu_indices(static_cast<size_t>(max_solutions) * slots_per_solution);

        int gpu_result_count = GpuStrategyEnumeration(
            all_attr_ids.data(), all_attr_values.data(),
            module_attr_counts.data(), module_offsets.data(),
            static_cast<int>(modules.size()), static_cast<int>(all_attr_ids.size()),
            target_attrs.empty() ? nullptr : target_attrs.data(), static_cast<int>(target_attrs.size()),
            exclude_attrs.empty() ? nullptr : exclude_attrs.data(), static_cast<int>(exclude_attrs.size()),
            min_attr_ids.empty() ? nullptr : min_attr_ids.data(),
            min_attr_values.empty() ? nullptr : min_attr_values.data(),
            static_cast<int>(min_attr_ids.size()),
            combo_size,
            max_solutions,
            gpu_scores.data(), gpu_indices.data());

        std::vector<ModuleSolution> final_solutions;
        final_solutions.reserve(gpu_result_count);
        for (int i = 0; i < gpu_result_count; ++i) {
            std::vector<ModuleInfo> sol_mods;
            std::vector<size_t> sol_indices;
            sol_indices.reserve(static_cast<size_t>(combo_size));
            // 每解 combo_size 个索引，按 slots_per_solution 个 long long 打包
            for (int slot = 0; slot < slots_per_solution; ++slot) {
                long long packed = gpu_indices[static_cast<size_t>(i) * slots_per_solution + slot];
                int in_slot = std::min(4, combo_size - slot * 4);
                for (int j = 0; j < in_slot; ++j) {
                    size_t module_idx = static_cast<size_t>((packed >> (j * 16)) & 0xFFFF);
                    if (module_idx < modules.size()) {
                        sol_mods.push_back(modules[module_idx]);
                        sol_indices.push_back(module_idx);
                    }
                }
            }
            if (static_cast<int>(sol_mods.size()) != combo_size ||
                static_cast<int>(sol_indices.size()) != combo_size) {
                continue;
            }
            int exact_score = CalculateCombatPowerByIndices(
                sol_indices, modules, target_attributes, exclude_attributes);
            auto result = CalculateCombatPower(sol_mods);
            if (exact_score != gpu_scores[i]) {
                printf("CUDA score mismatch: gpu=%d cpu=%d (solution #%d)\n",
                       gpu_scores[i], exact_score, i);
            }
            final_solutions.emplace_back(sol_mods, exact_score, result.second);
        }
        std::sort(final_solutions.begin(), final_solutions.end(),
                  [](const ModuleSolution& a, const ModuleSolution& b) {
                      return a.score > b.score;
                  });
        return final_solutions;
    }
    printf("CUDA not available, falling back to CPU\n");
#endif
    return StrategyEnumeration(modules, target_attributes, exclude_attributes,
                               min_attr_sum_requirements, max_solutions, max_workers, combo_size);
}

#ifdef USE_OPENCL
extern "C" int TestOpenCL();
#endif

std::vector<ModuleSolution> ModuleOptimizerCpp::StrategyEnumerationGPU(
    const std::vector<ModuleInfo>& modules,
    const std::unordered_set<int>& target_attributes,
    const std::unordered_set<int>& exclude_attributes,
    const std::unordered_map<int, int>& min_attr_sum_requirements,
    int max_solutions,
    int max_workers,
    int combo_size) {
#ifdef USE_CUDA
    if (TestCuda())
        return StrategyEnumerationCUDA(modules, target_attributes, exclude_attributes,
                                       min_attr_sum_requirements, max_solutions, max_workers, combo_size);
#endif
#ifdef USE_OPENCL
    if (TestOpenCL())
        return StrategyEnumerationOpenCL(modules, target_attributes, exclude_attributes,
                                         min_attr_sum_requirements, max_solutions, max_workers, combo_size);
#endif
    return StrategyEnumeration(modules, target_attributes, exclude_attributes,
                               min_attr_sum_requirements, max_solutions, max_workers, combo_size);
}

std::vector<ModuleSolution> ModuleOptimizerCpp::OptimizeModules(
    const std::vector<ModuleInfo>& modules,
    const std::unordered_set<int>& target_attributes,
    const std::unordered_set<int>& exclude_attributes,
    int max_solutions, int max_attempts_multiplier, int local_search_iterations,
    int combo_size) {

    auto candidate_modules = modules;
    std::vector<LightweightSolution> lightweight_solutions;
    std::set<std::vector<size_t>> seen_combinations;
    int max_attempts = max_solutions * max_attempts_multiplier, attempts = 0;
    while (lightweight_solutions.size() < static_cast<size_t>(max_solutions) && attempts < max_attempts) {
        ++attempts;
        auto solution = GreedyConstructSolutionByIndices(candidate_modules, target_attributes, exclude_attributes, combo_size);
        if (solution.module_indices.empty()) continue;
        auto improved = LocalSearchImproveByIndices(solution, candidate_modules, local_search_iterations, target_attributes, exclude_attributes);
        if (IsCombinationUnique(improved.module_indices, seen_combinations)) {
            auto si = improved.module_indices; std::sort(si.begin(), si.end());
            seen_combinations.insert(si); lightweight_solutions.push_back(improved);
        }
    }
    std::sort(lightweight_solutions.begin(), lightweight_solutions.end(),
              [](const auto& a, const auto& b){ return a.score > b.score; });
    std::vector<ModuleSolution> solutions;
    solutions.reserve(lightweight_solutions.size());
    for (const auto& lw : lightweight_solutions) {
        std::vector<ModuleInfo> sol_mods;
        for (size_t idx : lw.module_indices) sol_mods.push_back(candidate_modules[idx]);
        auto result = CalculateCombatPower(sol_mods);
        solutions.emplace_back(sol_mods, lw.score, result.second);
    }
    return solutions;
}

LightweightSolution ModuleOptimizerCpp::GreedyConstructSolutionByIndices(
    const std::vector<ModuleInfo>& modules,
    const std::unordered_set<int>& target_attributes,
    const std::unordered_set<int>& exclude_attributes,
    int combo_size) {
    if (modules.empty() || combo_size <= 0) {
        return LightweightSolution({}, 0);
    }
    combo_size = std::max(1, std::min(combo_size, static_cast<int>(modules.size())));
    std::random_device rd; std::mt19937 gen(rd());
    std::uniform_real_distribution<> dis(0.0, 1.0);
    std::uniform_int_distribution<> module_dis(0, static_cast<int>(modules.size()) - 1);
    std::vector<size_t> current_indices;
    current_indices.push_back(module_dis(gen));
    for (int step = 1; step < combo_size; ++step) {
        std::vector<size_t> candidates; std::vector<int> scores;
        for (size_t mi = 0; mi < modules.size(); ++mi) {
            bool dup = false;
            for (size_t ci : current_indices) { if (ci==mi){dup=true;break;} }
            if (dup) continue;
            auto test = current_indices; test.push_back(mi);
            candidates.push_back(mi);
            scores.push_back(CalculateCombatPowerByIndices(test, modules, target_attributes, exclude_attributes));
        }
        if (candidates.empty()) break;
        if (dis(gen) < 0.8) {
            current_indices.push_back(candidates[std::distance(scores.begin(), std::max_element(scores.begin(), scores.end()))]);
        } else {
            std::vector<std::pair<int,size_t>> sc;
            for (size_t i=0;i<candidates.size();++i) sc.emplace_back(scores[i],candidates[i]);
            std::sort(sc.begin(),sc.end(),[](auto&a,auto&b){return a.first>b.first;});
            int tc = std::min(3,(int)sc.size());
            current_indices.push_back(sc[std::uniform_int_distribution<>(0,tc-1)(gen)].second);
        }
    }
    return LightweightSolution(current_indices,
        CalculateCombatPowerByIndices(current_indices, modules, target_attributes, exclude_attributes));
}

LightweightSolution ModuleOptimizerCpp::LocalSearchImproveByIndices(
    const LightweightSolution& solution, const std::vector<ModuleInfo>& all_modules,
    int iterations, const std::unordered_set<int>& target_attributes, const std::unordered_set<int>& exclude_attributes) {
    LightweightSolution best = solution;
    std::random_device rd; std::mt19937 gen(rd());
    std::uniform_int_distribution<> module_dis(0, static_cast<int>(all_modules.size()) - 1);
    for (int it = 0; it < iterations; ++it) {
        bool improved = false;
        for (size_t i = 0; i < best.module_indices.size(); ++i) {
            std::vector<size_t> candidates;
            for (int j = 0; j < std::min(20,(int)all_modules.size()); ++j) candidates.push_back(module_dis(gen));
            for (size_t new_idx : candidates) {
                bool dup = false;
                for (size_t ei : best.module_indices) { if (ei==new_idx){dup=true;break;} }
                if (dup) continue;
                auto ni = best.module_indices; ni[i] = new_idx;
                int ns = CalculateCombatPowerByIndices(ni, all_modules, target_attributes, exclude_attributes);
                if (ns > best.score) { best = LightweightSolution(ni, ns); improved = true; break; }
            }
            if (improved) break;
        }
        if (!improved && it > iterations/2) break;
    }
    return best;
}

bool ModuleOptimizerCpp::IsCombinationUnique(
    const std::vector<size_t>& indices, const std::set<std::vector<size_t>>& seen_combinations) {
    auto si = indices; std::sort(si.begin(), si.end());
    return seen_combinations.find(si) == seen_combinations.end();
}
