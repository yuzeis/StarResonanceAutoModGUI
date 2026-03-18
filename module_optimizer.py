"""
模组搭配优化器 - 多策略并行, 使用C++进行核心运算
"""

import logging
import math
import os
import multiprocessing as mp
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass
from logging_config import get_logger
import psutil
from module_types import (
    ModuleInfo, ModulePart, ModuleCategory,
    MODULE_CATEGORY_MAP, ATTR_THRESHOLDS, BASIC_ATTR_POWER_MAP, SPECIAL_ATTR_POWER_MAP,
    TOTAL_ATTR_POWER_MAP, ATTR_NAME_TYPE_MAP, MODULE_ATTR_IDS,
    to_english_attr, to_english_module, CATEGORY_CN_TO_EN, tr
)
from cpp_extension.module_optimizer_cpp import (
    ModulePart as CppModulePart,
    ModuleInfo as CppModuleInfo,
    ModuleSolution as CppModuleSolution,
    strategy_enumeration_cpp,
    strategy_enumeration_cuda_cpp,
    strategy_enumeration_opencl_cpp,
    optimize_modules_cpp
)

# 多进程保护, 延迟初始化日志器
logger = None

def _get_logger():
    """延迟获取日志器"""
    global logger
    if logger is None:
        logger = get_logger(__name__)
    return logger


@dataclass
class ModuleSolution:
    """模组搭配解
    
    Attributes:
        modules: 模组列表
        score: 综合评分
        attr_breakdown: 属性分布
    """
    modules: List[ModuleInfo]
    score: float
    attr_breakdown: Dict[str, int]


class ModuleOptimizer:
    """模组搭配优化器"""
    
    def __init__(self, target_attributes: List[str] = None, exclude_attributes: List[str] = None,
                 min_attr_sum_requirements: dict | None = None, lang: str = 'zh',
                 combo_size: int = 4, compute_mode: str = 'cpu'):
        """初始化模组搭配优化器
        
        Args:
            target_attributes: 目标属性列表，用于优先筛选
            exclude_attributes: 排除属性列表, 用于权重为0
            combo_size: 组合件数 (1~10，默认4)
            compute_mode: 计算模式 ('cpu'/'cuda'/'opencl'，默认'cpu')
        """
        self.logger = _get_logger()
        self._result_log_file = None
        self.target_attributes = target_attributes or []
        self.exclude_attributes = exclude_attributes or []
        self.min_attr_sum_requirements = min_attr_sum_requirements or {}
        self.lang = (lang or 'zh').lower()
        self.combo_size = max(1, min(10, int(combo_size)))
        self.compute_mode = (compute_mode or 'cpu').lower()
        
        self.local_search_iterations = 50  # 局部搜索迭代次数
        self.max_attempts = 20             # 贪心+局部搜索最大尝试次数
        self.max_solutions = 100           # 最大解数量
        self.max_workers = 8               # 最大线程数
        self.enumeration_num = 400         # 并行策略中最大枚举模组数

    def _get_current_log_file(self) -> Optional[str]:
        """获取当前日志文件路径
        
        Returns:
            Optional[str]: 日志文件路径, 如果未找到则返回 None
            
        Raises:
            Exception: 获取日志文件路径时可能出现的异常
        """
        try:
            root_logger = logging.getLogger()
            for handler in root_logger.handlers:
                if isinstance(handler, logging.FileHandler):
                    return handler.baseFilename
            return None
        except Exception as e:
            self.logger.warning(f"无法获取日志文件路径: {e}")
            return None
    
    def _log_result(self, message: str):
        """记录筛选结果到日志文件
        
        Args:
            message: 要记录的消息内容
            
        Raises:
            Exception: 写入日志文件时可能出现的异常
        """
        try:
            if self._result_log_file is None:
                self._result_log_file = self._get_current_log_file()
            
            if self._result_log_file and os.path.exists(self._result_log_file):
                with open(self._result_log_file, 'a', encoding='utf-8') as f:
                    f.write(message + '\n')
        except Exception as e:
            self.logger.warning(f"记录筛选结果失败: {e}")

    def get_cpu_count(self) -> int:
        """获取CPU核心数"""
        try:
            return psutil.cpu_count(logical=True)
        except (NotImplementedError, OSError, RuntimeError):
            pass
        return 8

    def _output(self, msg_zh: str, msg_en: str):
        """同时 print + _log_result，双语选择"""
        msg = tr(self.lang, msg_zh, msg_en)
        print(msg)
        self._log_result(msg)

    def _filter_by_category(self, modules: List[ModuleInfo], category: ModuleCategory) -> Optional[List[ModuleInfo]]:
        """按类型过滤模组，数量不足 combo_size 时返回 None"""
        cat_disp = category.value if self.lang != 'en' else CATEGORY_CN_TO_EN.get(category.value, category.value)
        if category == ModuleCategory.ALL:
            filtered = modules
            self.logger.info(tr(self.lang,
                f"使用全部模组，共{len(filtered)}个",
                f"Using all modules, total={len(filtered)}"))
        else:
            filtered = [m for m in modules if self.get_module_category(m) == category]
            self.logger.info(tr(self.lang,
                f"找到{len(filtered)}个{category.value}类型模组",
                f"Found {len(filtered)} {cat_disp} modules"))
        if len(filtered) < self.combo_size:
            self.logger.warning(tr(self.lang,
                f"{category.value}类型模组数量不足{self.combo_size}个，无法形成完整搭配",
                f"Not enough {cat_disp} modules (<{self.combo_size}) to form a combination"))
            return None
        return filtered

    def _module_target_score(self, module: ModuleInfo) -> int:
        """计算单个模组的目标属性总值（受 target_attributes 过滤）"""
        if self.target_attributes:
            return sum(p.value for p in module.parts if p.name in self.target_attributes)
        return sum(p.value for p in module.parts)
    
    def _compute_enum_threshold(self, fast: bool = False) -> int:
        """根据 combo_size 和 compute_mode 动态计算枚举候选上限 N_max，
        满足 C(N_max, combo_size) ≤ 目标组合数预算。

        对枚举模式（fast=False）额外保证最低候选数:
            CPU ≥ 800, CUDA/OpenCL ≥ 1000
        确保枚举质量不低于硬编码版本。

        Args:
            fast: True → 非枚举模式辅助枚举预算（约2~5s）
                  False → 枚举模式主枚举预算（允许更长时间换取更高质量）

        Returns:
            int: 最大可枚举候选模组数 N
        """
        k = self.combo_size
        mode = self.compute_mode

        # (fast_budget, full_budget)，单位：组合数
        _BUDGETS: Dict[str, Tuple[int, int]] = {
            'cpu':    (150_000_000,   800_000_000),
            'cuda':   (600_000_000, 4_000_000_000),
            'opencl': (500_000_000, 2_500_000_000),
        }
        fast_budget, full_budget = _BUDGETS.get(mode, _BUDGETS['cpu'])
        target = fast_budget if fast else full_budget

        # k=1 枚举退化为线性扫描，无意义限制
        if k <= 1:
            return 100_000

        # 二分搜索：最大 N 使 C(N, k) ≤ target
        lo, hi, best = k, 5000, k + 1
        while lo <= hi:
            mid = (lo + hi) // 2
            try:
                c = math.comb(mid, k)
            except (OverflowError, ValueError):
                hi = mid - 1
                continue
            if c <= target:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        # 枚举模式保底：候选数不低于经验下限，与旧版硬编码一致
        if not fast:
            _MIN_FLOORS: Dict[str, int] = {
                'cpu':    800,
                'cuda':   1000,
                'opencl': 1000,
            }
            best = max(best, _MIN_FLOORS.get(mode, 800))

        return best

    def _build_attr_sets(self) -> Tuple[Set[int], Set[int]]:
        """构建目标属性ID集合与排除属性ID集合（两个策略函数共用）

        Returns:
            (target_attrs_set, exclude_attrs_set)
        """
        boost_names: Set[str] = set(self.target_attributes or [])
        if self.min_attr_sum_requirements:
            boost_names.update(self.min_attr_sum_requirements.keys())
        target_attrs_set = {MODULE_ATTR_IDS[a] for a in boost_names if a in MODULE_ATTR_IDS}
        exclude_attrs_set = {MODULE_ATTR_IDS[a] for a in self.exclude_attributes if a in MODULE_ATTR_IDS}
        return target_attrs_set, exclude_attrs_set

    def _finalize_solutions(self, solutions: List[ModuleSolution], top_n: int) -> List[ModuleSolution]:
        """去重、过滤、排序、截取、可选恢复原始评分（optimize_modules / enumerate_modules 共用收尾）

        Args:
            solutions: 原始解列表（可含重复）
            top_n: 最终返回上限

        Returns:
            List[ModuleSolution]: 处理完毕的解列表
        """
        unique = self._complete_deduplicate(solutions)
        unique = self._filter_by_min_attr(unique)
        unique.sort(key=lambda x: x.score, reverse=True)
        result = unique[:top_n]

        if self.target_attributes or self.min_attr_sum_requirements:
            result = self._restore_original_scores(result)
            result.sort(key=lambda x: x.score, reverse=True)
        return result

    def get_module_category(self, module: ModuleInfo) -> ModuleCategory:
        """获取模组类型分类
        
        Args:
            module: 模组信息对象
            
        Returns:
            ModuleCategory: 模组类型分类，默认为攻击类型
        """
        return MODULE_CATEGORY_MAP.get(module.config_id, ModuleCategory.ATTACK)
    
    def _prefilter_modules(self, modules: List[ModuleInfo]) -> Tuple[List[ModuleInfo], List[ModuleInfo]]:
        """预筛选模组，选择高质量候选
        
        Args:
            modules: 所有模组列表
            
        Returns:
            Tuple[List[ModuleInfo], List[ModuleInfo]]: (top_modules, candidate_modules)
                - top_modules: 第一轮筛选出的优质模组, 基于总属性值
                - candidate_modules: 第二轮筛选出的候选模组, 基于各属性值分布
        """
        # 基于总属性值
        top_modules = self._prefilter_modules_by_total_scores(modules, self.enumeration_num)
        
        attr_modules = {}
        target_set = set(self.target_attributes) if self.target_attributes else None
        for module in modules:
            for part in module.parts:
                if target_set and part.name not in target_set:
                    continue
                attr_modules.setdefault(part.name, []).append((module, part.value))
        
        attr_count = len(attr_modules)
        single_attr_num = 120 if attr_count <= 5 else 60

        candidate_modules = top_modules.copy()
        for attr_name, module_values in attr_modules.items():
            sorted_by_attr = sorted(module_values, key=lambda x: x[1], reverse=True)
            top_attr_modules = [item[0] for item in sorted_by_attr[:single_attr_num]]
            candidate_modules.extend(top_attr_modules)
        
        candidate_modules = list(set(candidate_modules))
        
        self.logger.info(tr(self.lang, 
            f"筛选后模组数量: candidate_modules={len(candidate_modules)} top_modules={len(top_modules)}",
            f"After prefilter: candidate_modules={len(candidate_modules)} top_modules={len(top_modules)}"
        ))
        attrs_disp = list(attr_modules.keys()) if self.lang != 'en' else [to_english_attr(a) for a in attr_modules.keys()]
        self.logger.info(tr(self.lang, f"涉及的属性类型: {list(attr_modules.keys())}", f"Involved attributes: {attrs_disp}"))
        return top_modules, candidate_modules
    
    def _prefilter_modules_by_total_scores(self, modules: List[ModuleInfo], num: int) -> List[ModuleInfo]:
        """预筛选模组，按目标属性总值取 top-N"""
        scored = sorted(modules, key=self._module_target_score, reverse=True)
        return scored[:num]

    def _prefilter_for_enumeration(self, modules: List[ModuleInfo], threshold: int) -> List[ModuleInfo]:
        """枚举模式专用的属性感知预筛选

        与非枚举模式的 _prefilter_modules 思路一致，但目标不同：
        - 非枚举模式：产出尽量多的候选给贪心（不受阈值限制）
        - 枚举模式：在 threshold 以内选出覆盖面最广的候选集

        策略：
        1. 总属性值 top-N 保底（占阈值的 60%）
        2. 各目标属性的单项 top 补充（确保关键属性上的强模组不遗漏）
        3. 合并去重后若仍超阈值，按综合评分截断
        """
        if len(modules) <= threshold:
            return modules

        # 总分 top（占阈值的 60%）
        top_by_total_n = max(threshold * 3 // 5, 1)
        top_by_total = self._prefilter_modules_by_total_scores(modules, top_by_total_n)

        # 各属性 top
        target_set = set(self.target_attributes) if self.target_attributes else None
        attr_modules: Dict[str, List[Tuple]] = {}
        for module in modules:
            for part in module.parts:
                if target_set and part.name not in target_set:
                    continue
                attr_modules.setdefault(part.name, []).append((module, part.value))

        # 每属性分配的候选数：剩余名额平均分配
        attr_count = max(len(attr_modules), 1)
        remaining_slots = threshold - len(top_by_total)
        single_attr_num = max(remaining_slots // attr_count, 20)

        combined: Set[ModuleInfo] = set(top_by_total)
        for attr_name, module_values in attr_modules.items():
            sorted_by_attr = sorted(module_values, key=lambda x: x[1], reverse=True)
            for item in sorted_by_attr[:single_attr_num]:
                combined.add(item[0])

        # 超阈值时按综合评分截断
        if len(combined) > threshold:
            combined_list = sorted(combined, key=self._module_target_score, reverse=True)
            result = combined_list[:threshold]
        else:
            result = list(combined)

        self.logger.info(tr(self.lang,
            f"枚举预筛选: 总分top={len(top_by_total)} + 属性top(×{attr_count}属性,每属性≤{single_attr_num}) "
            f"→ 合并去重={len(combined)} → 截断至{len(result)}",
            f"Enum prefilter: total-top={len(top_by_total)} + attr-top(×{attr_count} attrs, ≤{single_attr_num} each) "
            f"→ merged={len(combined)} → capped to {len(result)}"))
        return result
    
    def optimize_modules(self, modules: List[ModuleInfo], category: ModuleCategory, top_n: int = 40) -> List[ModuleSolution]:
        """优化模组搭配（非枚举模式）

        策略：贪心+局部搜索（主力）+ 辅助枚举（动态规模）并用。
        辅助枚举规模由 _compute_enum_threshold(fast=True) 动态确定，
        使枚举部分始终控制在合理时间预算内（CPU ~2-5s，GPU更短）。

        修复说明：
        - 原来 else 分支 greedy_solutions=[] 从不赋值（贪心失效 bug）已修复
        - 原来枚举阈值硬编码为 400，现改为基于 combo_size/mode 动态计算
        - 两种策略现在始终都运行，通过结果合并取最优

        Args:
            modules: 所有模组列表
            category: 目标模组类型
            top_n: 返回前N个最优解，默认40

        Returns:
            List[ModuleSolution]: 最优解列表
        """
        cat_disp = category.value if self.lang != 'en' else CATEGORY_CN_TO_EN.get(category.value, category.value)
        cpu_n = self.get_cpu_count()
        self.logger.info(tr(self.lang,
            f"开始优化{category.value}类型模组搭配（非枚举模式），cpu_count={cpu_n}",
            f"Start optimizing {cat_disp} modules (non-enum mode), cpu_count={cpu_n}"))

        # ── 1. 按类型过滤 ────────────────────────────────────────────────────
        filtered_modules = self._filter_by_category(modules, category)
        if filtered_modules is None:
            return []

        # ── 2. 动态枚举阈值（非枚举模式使用快速预算）────────────────────────
        enum_threshold = self._compute_enum_threshold(fast=True)

        # ── 3. 枚举候选：按总分取 top-N，N = min(实际数量, 动态阈值) ─────────
        if len(filtered_modules) > enum_threshold:
            enum_modules = self._prefilter_modules_by_total_scores(filtered_modules, enum_threshold)
        else:
            enum_modules = filtered_modules

        # ── 4. 贪心候选：属性感知筛选，覆盖更广（可超过枚举阈值）────────────
        _, candidate_modules = self._prefilter_modules(filtered_modules)

        # ── 5. 记录本次枚举规模 ──────────────────────────────────────────────
        actual_combos = math.comb(len(enum_modules), self.combo_size)
        self.logger.info(tr(self.lang,
            f"辅助枚举: {len(enum_modules)}个候选  C({len(enum_modules)},{self.combo_size})={actual_combos:,}组合  "
            f"贪心: {len(candidate_modules)}个候选  (枚举阈值N≤{enum_threshold})",
            f"Aux-enum: {len(enum_modules)} candidates  C({len(enum_modules)},{self.combo_size})={actual_combos:,} combos  "
            f"Greedy: {len(candidate_modules)} candidates  (enum threshold N≤{enum_threshold})"))

        # ── 6. 选择执行方式：并行 vs 顺序 ────────────────────────────────────
        # 两个任务都足够重才开进程池（进程 spawn 开销约 0.3~0.8s）
        _PARALLEL_MIN = 5_000_000  # 低于500万组合不值得开进程
        use_parallel = (actual_combos > _PARALLEL_MIN and len(candidate_modules) > 50)

        if use_parallel:
            self.logger.info(tr(self.lang,
                "并行执行：贪心+局部搜索 / 辅助枚举",
                "Parallel: greedy+local-search / aux-enumeration"))
            ctx = mp.get_context('spawn')
            with ctx.Pool(processes=min(2, mp.cpu_count())) as pool:
                greedy_future = pool.apply_async(self._strategy_greedy_local_search, (candidate_modules,))
                enum_future   = pool.apply_async(self._strategy_enumeration, (enum_modules,))
                greedy_solutions = greedy_future.get()
                enum_solutions   = enum_future.get()
        else:
            self.logger.info(tr(self.lang,
                "顺序执行：枚举 → 贪心+局部搜索",
                "Sequential: enumeration → greedy+local-search"))
            enum_solutions   = self._strategy_enumeration(enum_modules)
            greedy_solutions = self._strategy_greedy_local_search(candidate_modules)

        result = self._finalize_solutions(greedy_solutions + enum_solutions, top_n)
        self.logger.info(tr(self.lang,
            f"优化完成，返回{len(result)}个最优解",
            f"Optimization finished, returning {len(result)} best solutions"))
        return result

    def _filter_by_min_attr(self, solutions: List[ModuleSolution]) -> List[ModuleSolution]:
        """按硬性总和约束过滤解；约束来自 self.min_attr_sum_requirements（键为中文属性名）"""
        if not self.min_attr_sum_requirements:
            return solutions
        req = self.min_attr_sum_requirements
        out = []
        for s in solutions:
            bd = getattr(s, "attr_breakdown", {}) or {}
            ok = True
            for k, v in req.items():
                if bd.get(k, 0) < v:
                    ok = False
                    break
            if ok:
                out.append(s)
        return out


    def enumerate_modules(self, modules: List[ModuleInfo], category: ModuleCategory,
                          top_n: int = 40, add_greedy: bool = True) -> List[ModuleSolution]:
        """枚举模式

        add_greedy=True（默认）: 大枚举 + 贪心补充，合并取最优
        add_greedy=False: 纯枚举（完全枚举模式）

        Args:
            modules: 所有模组列表
            category: 目标模组类型
            top_n: 返回前N个最优解，默认40
            add_greedy: 是否追加贪心+局部搜索结果

        Returns:
            List[ModuleSolution]: 最优解列表
        """
        cat_disp = category.value if self.lang != 'en' else CATEGORY_CN_TO_EN.get(category.value, category.value)
        cpu_n = self.get_cpu_count()
        self.logger.info(tr(self.lang,
            f"开始优化{category.value}类型模组搭配（枚举模式, greedy={'开' if add_greedy else '关'}），cpu_count={cpu_n}",
            f"Start optimizing {cat_disp} modules (enum mode, greedy={'on' if add_greedy else 'off'}), cpu_count={cpu_n}"))

        # ── 1. 按类型过滤 ────────────────────────────────────────────────────
        filtered_modules = self._filter_by_category(modules, category)
        if filtered_modules is None:
            return []

        # ── 2. 动态枚举阈值（枚举模式，含保底 CPU≥800 / GPU≥1000）──────────
        enum_threshold = self._compute_enum_threshold(fast=False)

        # ── 3. 属性感知预筛选（总分top + 各属性top 合并）─────────────────────
        if len(filtered_modules) > enum_threshold:
            enum_modules = self._prefilter_for_enumeration(filtered_modules, enum_threshold)
        else:
            enum_modules = filtered_modules

        actual_combos = math.comb(len(enum_modules), self.combo_size)
        self.logger.info(tr(self.lang,
            f"枚举规模: {len(enum_modules)}个候选  "
            f"C({len(enum_modules)},{self.combo_size})={actual_combos:,}组合  "
            f"(阈值N≤{enum_threshold})",
            f"Enum scale: {len(enum_modules)} candidates  "
            f"C({len(enum_modules)},{self.combo_size})={actual_combos:,} combos  "
            f"(threshold N≤{enum_threshold})"))

        enum_solutions = self._strategy_enumeration(enum_modules)

        # ── 4. 可选：贪心+局部搜索补充 ───────────────────────────────────────
        if add_greedy:
            _, candidate_modules = self._prefilter_modules(filtered_modules)
            self.logger.info(tr(self.lang,
                f"贪心补充: {len(candidate_modules)}个候选",
                f"Greedy supplement: {len(candidate_modules)} candidates"))
            greedy_solutions = self._strategy_greedy_local_search(candidate_modules)
            all_solutions = enum_solutions + greedy_solutions
        else:
            all_solutions = enum_solutions

        result = self._finalize_solutions(all_solutions, top_n)
        self.logger.info(tr(self.lang,
            f"枚举完成，返回{len(result)}个最优解",
            f"Enumeration finished, returning {len(result)} best solutions"))
        return result

    def _strategy_enumeration(self, modules: List[ModuleInfo]) -> List[ModuleSolution]:
        """枚举（支持 combo_size 1~10，计算模式 cpu/cuda/opencl）"""

        cpp_modules = self._convert_to_cpp_modules(modules)

        target_attrs_set, exclude_attrs_set = self._build_attr_sets()
        # 属性总和约束（仅枚举路径需要传入 C++ 侧过滤）
        min_attr_id_requirements: Dict[int, int] = {
            MODULE_ATTR_IDS[name]: int(val)
            for name, val in self.min_attr_sum_requirements.items()
            if name in MODULE_ATTR_IDS
        }

        k     = self.combo_size
        mode  = self.compute_mode

        self.logger.info(tr(self.lang, 
            f"枚举模式: {mode.upper()}，{k}件套，共{len(modules)}个模组",
            f"Enumeration: mode={mode.upper()}, combo_size={k}, modules={len(modules)}"
        ))

        # ── 选择 C++ 后端 ────────────────────────────────────────────────────
        if mode == 'cuda':
            try:
                cpp_solutions = strategy_enumeration_cuda_cpp(
                    cpp_modules, target_attrs_set, exclude_attrs_set,
                    min_attr_id_requirements, self.max_solutions,
                    self.get_cpu_count(), k)
                return self._convert_from_cpp_solutions(cpp_solutions)
            except Exception as e:
                self.logger.warning(tr(self.lang, 
                    f"CUDA枚举失败，回退CPU: {e}", f"CUDA failed, fallback to CPU: {e}"))
                mode = 'cpu'

        if mode == 'opencl':
            try:
                cpp_solutions = strategy_enumeration_opencl_cpp(
                    cpp_modules, target_attrs_set, exclude_attrs_set,
                    min_attr_id_requirements, self.max_solutions,
                    self.get_cpu_count(), k)
                return self._convert_from_cpp_solutions(cpp_solutions)
            except Exception as e:
                self.logger.warning(tr(self.lang, 
                    f"OpenCL枚举失败，回退CPU: {e}", f"OpenCL failed, fallback to CPU: {e}"))
                mode = 'cpu'

        # CPU（默认 / 回退）
        cpp_solutions = strategy_enumeration_cpp(
            cpp_modules, target_attrs_set, exclude_attrs_set,
            min_attr_id_requirements, self.max_solutions,
            self.get_cpu_count(), k)
        return self._convert_from_cpp_solutions(cpp_solutions)
    
    def _strategy_greedy_local_search(self, modules: List[ModuleInfo]) -> List[ModuleSolution]:
        """贪心+局部搜索
        
        Args:
            modules: 所有模组列表
            
        Returns:
            List[ModuleSolution]: 最优解列表
        """
        
        if self.compute_mode != 'cpu':
            self.logger.info(tr(self.lang, 
                f"贪心+局部搜索阶段当前使用CPU后端（compute_mode={self.compute_mode} 仅影响枚举阶段）",
                f"Greedy/local-search phase currently uses CPU backend (compute_mode={self.compute_mode} affects enumeration only)"
            ))

        cpp_modules = self._convert_to_cpp_modules(modules)

        target_attrs_set, exclude_attrs_set = self._build_attr_sets()

        cpp_solutions = optimize_modules_cpp(
            cpp_modules, target_attrs_set, exclude_attrs_set,
            self.max_solutions, self.max_attempts, self.local_search_iterations,
            self.combo_size)
        
        result = self._convert_from_cpp_solutions(cpp_solutions)
        
        return result
    
    def _complete_deduplicate(self, solutions: List[ModuleSolution]) -> List[ModuleSolution]:
        """模组去重++
        
        Args:
            modules: 模组列表
            
        Returns:
            List: 去重后的模组列表
        """
                
        unique_solutions = []
        seen_combinations = set()
        
        for solution in solutions:
            module_ids = tuple(sorted([module.uuid for module in solution.modules]))
            if module_ids not in seen_combinations:
                seen_combinations.add(module_ids)
                unique_solutions.append(solution)
        
        return unique_solutions
    
    def _convert_to_cpp_modules(self, modules: List[ModuleInfo]) -> List:
        """python数据结构转C++
        
        Args:
            modules: 模组列表
            
        Returns:
            List: C++模组结构
        """

        cpp_modules = []
        for module in modules:
            cpp_parts = []
            for part in module.parts:
                cpp_parts.append(CppModulePart(int(part.id), part.name, int(part.value)))
            cpp_modules.append(CppModuleInfo(
                module.name, module.config_id, module.uuid, 
                module.quality, cpp_parts
            ))
        return cpp_modules
    
    def _convert_from_cpp_solutions(self, cpp_solutions: List) -> List[ModuleSolution]:
        """C++数据结构转python
        
        Args:
            modules: 模组列表
            
        Returns:
            List: python模组结构
        """
        
        solutions = []
        for cpp_solution in cpp_solutions:
            modules = []
            for cpp_module in cpp_solution.modules:
                parts = []
                for cpp_part in cpp_module.parts:
                    parts.append(ModulePart(cpp_part.id, cpp_part.name, cpp_part.value))
                modules.append(ModuleInfo(
                    cpp_module.name, cpp_module.config_id, cpp_module.uuid,
                    cpp_module.quality, parts
                ))
            
            solutions.append(ModuleSolution(
                modules, cpp_solution.score, cpp_solution.attr_breakdown
            ))
        return solutions
    
    def _restore_original_scores(self, solutions: List[ModuleSolution]) -> List[ModuleSolution]:
        """恢复原始评分
        
        Args:
            solutions: 包含双倍评分的解决方案列表
            
        Returns:
            List[ModuleSolution]: 恢复原始评分的解决方案列表
        """
        
        restored_solutions = []
        for solution in solutions:
            # 重新计算原始评分
            attr_breakdown = {}
            for module in solution.modules:
                for part in module.parts:
                    attr_breakdown[part.name] = attr_breakdown.get(part.name, 0) + part.value
            
            # 计算原始战斗力
            threshold_power = 0
            total_attr_value = 0
            
            for attr_name, attr_value in attr_breakdown.items():
                total_attr_value += attr_value
                
                # 计算属性等级
                max_level = 0
                for i, threshold in enumerate(ATTR_THRESHOLDS):
                    if attr_value >= threshold:
                        max_level = i + 1
                    else:
                        break
                
                if max_level > 0:
                    attr_type = ATTR_NAME_TYPE_MAP.get(attr_name, "basic")
                    if attr_type == "special":
                        threshold_power += SPECIAL_ATTR_POWER_MAP.get(max_level, 0)
                    else:
                        threshold_power += BASIC_ATTR_POWER_MAP.get(max_level, 0)
            
            # 计算总属性战斗力 — 与 C++ 一致: capped = min(total, 120)
            capped_total = min(total_attr_value, 120)
            total_attr_power = TOTAL_ATTR_POWER_MAP.get(capped_total, 0)
            original_score = threshold_power + total_attr_power
            
            restored_solutions.append(ModuleSolution(
                solution.modules, original_score, attr_breakdown
            ))
        
        return restored_solutions
    
    def print_solution_details(self, solution: ModuleSolution, rank: int):
        """打印解详细信息
        
        Args:
            solution: 模组搭配解
            rank: 排名
            
        Note:
            同时输出到控制台和日志文件
        """
        if self.lang == 'en':
            print(f"\n=== Rank #{rank} Solution ===")
            self._log_result(f"\n=== Rank #{rank} Solution ===")
        else:
            print(f"\n=== 第{rank}名搭配 ===")
            self._log_result(f"\n=== 第{rank}名搭配 ===")
        
        total_value = sum(solution.attr_breakdown.values())
        self._output(f"总属性值: {total_value}", f"Total Attribute Value: {total_value}")
        self._output(f"战斗力: {solution.score:.2f}", f"Score: {solution.score:.2f}")
        
        self._output("\n模组列表:", "\nModules:")
        for i, module in enumerate(solution.modules, 1):
            if self.lang == 'en':
                parts_str = ", ".join([f"{to_english_attr(p.name)}+{p.value}" for p in module.parts])
                name_disp = to_english_module(module.config_id, module.name)
                self._output("", f"  {i}. {name_disp} (Quality {module.quality}) - {parts_str}")
            else:
                parts_str = ", ".join([f"{p.name}+{p.value}" for p in module.parts])
                self._output(f"  {i}. {module.name} (品质{module.quality}) - {parts_str}", "")
        
        self._output("\n属性分布:", "\nAttribute Breakdown:")
        for attr_name, value in sorted(solution.attr_breakdown.items()):
            if self.lang == 'en':
                self._output("", f"  {to_english_attr(attr_name)}: +{value}")
            else:
                self._output(f"  {attr_name}: +{value}", "")
    
    def optimize_and_display(self, 
                           modules: List[ModuleInfo], 
                           category: ModuleCategory = ModuleCategory.ALL,
                           top_n: int = 40,
                           enumeration_mode: bool = False,
                           full_enumeration_mode: bool = False):
        """优化并显示结果
        
        Args:
            modules: 所有模组列表
            category: 目标模组类型，默认全部
            top_n: 显示前N个最优解, 默认40
            enumeration_mode: 枚举模式（大枚举+贪心合并取最优）
            full_enumeration_mode: 完全枚举模式（纯枚举，无贪心补充）
        """
        sep = f"\n{'='*50}"
        self._output(sep, sep)
        cat_disp = category.value if self.lang != 'en' else CATEGORY_CN_TO_EN.get(category.value, category.value)
        self._output(f"模组搭配优化 - {category.value}类型", f"Module Optimization - {cat_disp}")
        self._output(f"{'='*50}", f"{'='*50}")
        
        if full_enumeration_mode:
            self._output("策略: 完全枚举", "Strategy: Full Enumeration")
            optimal_solutions = self.enumerate_modules(modules, category, top_n, add_greedy=False)
        elif enumeration_mode:
            self._output("策略: 枚举+贪心", "Strategy: Enumeration + Greedy")
            optimal_solutions = self.enumerate_modules(modules, category, top_n, add_greedy=True)
        else:
            optimal_solutions = self.optimize_modules(modules, category, top_n)
        
        if not optimal_solutions:
            self._output(f"未找到{category.value}类型的有效搭配",
                         f"No valid combinations found for {cat_disp}")
            return
        
        self._output(f"\n找到{len(optimal_solutions)}个最优搭配:",
                     f"\nTop combinations found: {len(optimal_solutions)}")
        
        for i, solution in enumerate(optimal_solutions, 1):
            self.print_solution_details(solution, i)
        
        # 显示统计信息
        cat_count = len([m for m in modules if self.get_module_category(m) == category])
        self._output(sep, sep)
        self._output("统计信息:", "Statistics:")
        self._output(f"总模组数量: {len(modules)}", f"Total modules: {len(modules)}")
        self._output(f"{category.value}类型模组: {cat_count}", f"{cat_disp} modules: {cat_count}")
        self._output(f"最高战斗力: {optimal_solutions[0].score:.2f}",
                     f"Highest score: {optimal_solutions[0].score:.2f}")
        self._output(f"{'='*50}", f"{'='*50}")
