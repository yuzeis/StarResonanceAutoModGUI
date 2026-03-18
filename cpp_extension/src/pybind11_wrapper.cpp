#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>

#include "module_optimizer.h"

// 前向声明 GPU 可用性测试函数
#ifdef USE_CUDA
extern "C" int TestCuda();
#endif
#ifdef USE_OPENCL
extern "C" int TestOpenCL();
#endif

namespace py = pybind11;

PYBIND11_MODULE(module_optimizer_cpp, m) {
    m.doc() = "星痕共鸣模组优化器 C++ 扩展（支持 combo_size 1~10）";

    py::class_<ModulePart>(m, "ModulePart")
        .def(py::init<int, std::string, int>())
        .def_readwrite("id",    &ModulePart::id)
        .def_readwrite("name",  &ModulePart::name)
        .def_readwrite("value", &ModulePart::value);

    py::class_<ModuleInfo>(m, "ModuleInfo")
        .def(py::init<std::string, int, int, int, std::vector<ModulePart>>())
        .def_readwrite("name",      &ModuleInfo::name)
        .def_readwrite("config_id", &ModuleInfo::config_id)
        .def_readwrite("uuid",      &ModuleInfo::uuid)
        .def_readwrite("quality",   &ModuleInfo::quality)
        .def_readwrite("parts",     &ModuleInfo::parts);

    py::class_<ModuleSolution>(m, "ModuleSolution")
        .def_readwrite("modules",        &ModuleSolution::modules)
        .def_readwrite("score",          &ModuleSolution::score)
        .def_readwrite("attr_breakdown", &ModuleSolution::attr_breakdown);

    // ── CPU 多线程枚举（支持 combo_size 1~10）
    m.def("strategy_enumeration_cpp", &ModuleOptimizerCpp::StrategyEnumeration,
        "CPU多线程枚举，支持combo_size 1~10",
        py::arg("modules"),
        py::arg("target_attributes")        = std::unordered_set<int>{},
        py::arg("exclude_attributes")       = std::unordered_set<int>{},
        py::arg("min_attr_sum_requirements")= std::unordered_map<int,int>{},
        py::arg("max_solutions")            = 60,
        py::arg("max_workers")              = 8,
        py::arg("combo_size")               = 4);

    // ── CUDA GPU 枚举（支持 combo_size 1~10）
    m.def("strategy_enumeration_cuda_cpp", &ModuleOptimizerCpp::StrategyEnumerationCUDA,
        "CUDA GPU加速枚举，支持combo_size 1~10",
        py::arg("modules"),
        py::arg("target_attributes")        = std::unordered_set<int>{},
        py::arg("exclude_attributes")       = std::unordered_set<int>{},
        py::arg("min_attr_sum_requirements")= std::unordered_map<int,int>{},
        py::arg("max_solutions")            = 60,
        py::arg("max_workers")              = 8,
        py::arg("combo_size")               = 4);

    // ── OpenCL GPU 枚举（支持 combo_size 1~10）
    m.def("strategy_enumeration_opencl_cpp", &ModuleOptimizerCpp::StrategyEnumerationOpenCL,
        "OpenCL GPU加速枚举，支持combo_size 1~10",
        py::arg("modules"),
        py::arg("target_attributes")        = std::unordered_set<int>{},
        py::arg("exclude_attributes")       = std::unordered_set<int>{},
        py::arg("min_attr_sum_requirements")= std::unordered_map<int,int>{},
        py::arg("max_solutions")            = 60,
        py::arg("max_workers")              = 8,
        py::arg("combo_size")               = 4);

    m.def("optimize_modules_cpp", &ModuleOptimizerCpp::OptimizeModules,
        "贪心+局部搜索",
        py::arg("modules"),
        py::arg("target_attributes")        = std::unordered_set<int>{},
        py::arg("exclude_attributes")       = std::unordered_set<int>{},
        py::arg("max_solutions")            = 60,
        py::arg("max_attempts_multiplier")  = 20,
        py::arg("local_search_iterations")  = 30,
        py::arg("combo_size")               = 4);

#ifdef USE_CUDA
    m.def("test_cuda", []() -> int { return TestCuda(); }, "检测CUDA是否可用");
#else
    m.def("test_cuda", []() -> int { return 0; }, "检测CUDA是否可用");
#endif

#ifdef USE_OPENCL
    m.def("test_opencl", []() -> int { return TestOpenCL(); }, "检测OpenCL是否可用");
#else
    m.def("test_opencl", []() -> int { return 0; }, "检测OpenCL是否可用");
#endif
}
