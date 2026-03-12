import os
import sys
import re
import tempfile
import subprocess
from setuptools import setup, Extension
from pybind11.setup_helpers import Pybind11Extension
import pybind11
from pathlib import Path

# 检测 OpenCL 
def find_opencl():
    """查找OpenCL"""
    fixed_cuda = next(
        (r'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\{}'.format(v)
         for v in ['v13.2', 'v13.1', 'v13.0', 'v12.8', 'v12.6', 'v12.4', 'v12.3', 'v12.2', 'v12.1', 'v12.0']
         if os.path.exists(r'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\{}'.format(v))),
        r'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.2'
    )
    candidates = [{
        'include': os.path.join(fixed_cuda, 'include'),
        'libdir': os.path.join(fixed_cuda, 'lib', 'x64'),
        'lib': os.path.join(fixed_cuda, 'lib', 'x64', 'OpenCL.lib')
    }]

    env_home = os.environ.get('OPENCL_HOME')
    if env_home:
        candidates.append({
            'include': os.path.join(env_home, 'include'),
            'libdir': os.path.join(env_home, 'lib', 'x64'),
            'lib': os.path.join(env_home, 'lib', 'x64', 'OpenCL.lib')
        })
    cuda_env_home = os.environ.get('CUDA_HOME') or os.environ.get('CUDA_PATH')
    if cuda_env_home:
        candidates.append({
            'include': os.path.join(cuda_env_home, 'include'),
            'libdir': os.path.join(cuda_env_home, 'lib', 'x64'),
            'lib': os.path.join(cuda_env_home, 'lib', 'x64', 'OpenCL.lib')
        })

    for c in candidates:
        if c['include'] and os.path.exists(c['include']) and os.path.exists(c['lib']):
            print(f"✅ 找到OpenCL构建依赖: include={c['include']} lib={c['lib']}")
            return c
    print("⚠️ 未找到OpenCL构建依赖(跳过OpenCL支持).")
    return None

def find_cuda():
    """查找CUDA安装路径"""
    try:
        result = subprocess.run(['nvcc', '--version'], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ 找到CUDA编译器")
            cuda_paths = [
                os.environ.get('CUDA_HOME'),
                os.environ.get('CUDA_PATH'),
            ]
            cuda_base = r'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
            if os.path.exists(cuda_base):
                versions = sorted(os.listdir(cuda_base), reverse=True)
                cuda_paths += [os.path.join(cuda_base, v) for v in versions]
            
            cuda_home = None
            for path in cuda_paths:
                if path and os.path.exists(path):
                    cuda_home = path
                    break
            
            if cuda_home and os.path.exists(cuda_home):
                print(f"✅ CUDA路径: {cuda_home}")
                return cuda_home
            else:
                print("❌ 未找到CUDA安装路径")
                return None
                
    except FileNotFoundError:
        print("❌ 未找到nvcc编译器")
        return None

def find_vs_env_script():
    """查找可用的 Visual Studio/MSVC 环境脚本，兼容 BuildTools/Community/Professional/Enterprise。"""
    env_candidates = [
        os.environ.get("VCVARS64_BAT"),
        os.environ.get("VSDEVCMD_BAT"),
    ]
    for path in env_candidates:
        if path and os.path.exists(path):
            print(f"✅ 使用环境变量指定的VS脚本: {path}")
            return path

    vswhere_paths = [
        r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe",
        r"C:\Program Files\Microsoft Visual Studio\Installer\vswhere.exe",
    ]
    for vswhere in vswhere_paths:
        if not os.path.exists(vswhere):
            continue
        try:
            install_path = subprocess.check_output(
                [
                    vswhere,
                    "-latest",
                    "-products", "*",
                    "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property", "installationPath",
                ],
                text=True,
                encoding="utf-8",
                errors="ignore",
            ).strip()
            if install_path:
                dynamic_candidates = [
                    os.path.join(install_path, "VC", "Auxiliary", "Build", "vcvars64.bat"),
                    os.path.join(install_path, "Common7", "Tools", "VsDevCmd.bat"),
                ]
                for path in dynamic_candidates:
                    if os.path.exists(path):
                        print(f"✅ 通过vswhere找到VS脚本: {path}")
                        return path
        except Exception as e:
            print(f"⚠️ vswhere探测失败: {e}")

    fixed_candidates = [
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvars64.bat",
    ]
    for path in fixed_candidates:
        if os.path.exists(path):
            print(f"✅ 通过固定路径找到VS脚本: {path}")
            return path

    print("❌ 未找到Visual Studio环境脚本")
    return None


def get_supported_gencode_flags(cuda_home):
    """
    根据 nvcc 实际支持的架构动态生成 -gencode 参数。
    sm_120 (RTX 5000/Blackwell) 需要 CUDA 12.8+，低版本会静默失败，故做版本检测。
    """
    arch_list = [
        (75,  75,  10, 0),   # RTX 2000系列
        (86,  86,  11, 1),   # RTX 3000系列
        (89,  89,  11, 8),   # RTX 4000系列
        (120, 120, 12, 8),   # RTX 5000系列 (Blackwell) — 需要 CUDA 12.8+
    ]

    nvcc_major, nvcc_minor = 0, 0
    try:
        ver_result = subprocess.run(
            ['nvcc', '--version'], capture_output=True, text=True
        )
        for line in ver_result.stdout.splitlines():
            if 'release' in line:
                m = re.search(r'release\s+(\d+)\.(\d+)', line)
                if m:
                    nvcc_major, nvcc_minor = int(m.group(1)), int(m.group(2))
                    print(f"ℹ️  检测到 nvcc 版本: {nvcc_major}.{nvcc_minor}")
                    break
    except Exception as e:
        print(f"⚠️ 无法解析 nvcc 版本: {e}")

    flags = []
    for compute, sm, min_major, min_minor in arch_list:
        if (nvcc_major, nvcc_minor) >= (min_major, min_minor):
            flags.append(f'-gencode=arch=compute_{compute},code=sm_{sm}')
        else:
            print(f"⚠️ 跳过 sm_{sm}（需要 CUDA {min_major}.{min_minor}+，当前 {nvcc_major}.{nvcc_minor}）")

    if not flags:
        flags.append('-gencode=arch=compute_75,code=sm_75')

    print(f"ℹ️  启用的 GPU 架构: {' '.join(flags)}")
    return ' '.join(flags)


def compile_cuda_code(cuda_home):
    """编译CUDA代码为目标文件"""
    try:
        vs_vars = find_vs_env_script()
        if vs_vars is None:
            return False

        cuda_files = [
            ("src/module_optimizer_cuda.cu", "src/module_optimizer_cuda.obj")
        ]

        gencode_flags = get_supported_gencode_flags(cuda_home)

        all_compiled = True
        for src_file, obj_file in cuda_files:
            cuda_inner = (
                f'nvcc -c "{src_file}" -o "{obj_file}" -std=c++17 '
                f'--compiler-options "/O2,/std:c++17,/EHsc,/wd4819,/MD,/utf-8" '
                f'-Xcompiler "/Zc:preprocessor" --use_fast_math '
                f'-I"{cuda_home}\\include" -I"{pybind11.get_include()}" -Isrc '
                f'{gencode_flags}'
            )
            cuda_cmd = f'call "{vs_vars}" && {cuda_inner}'

            print(f"🔧 编译 {src_file} ...")
            print(f"📋 编译命令: {cuda_cmd}")

            # 将命令写入临时 .bat 文件再用 cmd /c 执行。
            # 这是处理带空格的嵌套引号路径最可靠的方式，
            # 完全规避 cmd /s /c 对引号的各种剥离/解析问题。
            bat_fd, bat_path = tempfile.mkstemp(suffix=".bat")
            try:
                with os.fdopen(bat_fd, 'w', encoding='gbk') as f:
                    f.write("@echo off\r\n")
                    f.write(cuda_cmd + "\r\n")
                result = subprocess.run(
                    ["cmd", "/c", bat_path],
                    capture_output=True,
                    encoding="gbk",   # Windows CMD 默认 GBK，防止中文乱码
                    errors="replace",
                )
            finally:
                try:
                    os.remove(bat_path)
                except OSError:
                    pass

            if result.returncode != 0:
                print(f"❌ {src_file} 编译失败:")
                print(f"stdout: {result.stdout or '(空)'}")
                print(f"stderr: {result.stderr or '(空)'}")
                all_compiled = False
                break
            else:
                print(f"✅ {src_file} 编译成功")

        if not all_compiled:
            return False

        print("✅ 所有CUDA文件编译成功")
        return True

    except Exception as e:
        print(f"❌ CUDA编译出错: {e}")
        return False

# 检查CUDA支持
force_cuda = os.environ.get('FORCE_CUDA') == '1'
force_cpu = os.environ.get('FORCE_CPU') == '1'

if force_cpu:
    print("🔧 强制CPU模式: 跳过CUDA检测")
    cuda_home = None
    use_cuda = False
elif force_cuda:
    print("🔧 强制CUDA模式: 检测CUDA环境")
    cuda_home = find_cuda()
    use_cuda = cuda_home is not None
    if not use_cuda:
        print("❌ 强制CUDA模式失败: CUDA环境不可用")
        print("💡 请安装CUDA Toolkit或使用 --version cpu 打包CPU版本")
        sys.exit(1)
else:
    cuda_home = find_cuda()
    use_cuda = cuda_home is not None

# 编译参数
is_windows = os.name == 'nt'
if is_windows:
    extra_compile_args = ["/O2", "/std:c++17", "/utf-8", "/EHsc", "/bigobj", "/MD"]
    extra_link_args = ["/NODEFAULTLIB:LIBCMT"]
else:
    extra_compile_args = ["-O3", "-march=native", "-std=c++17"]
    extra_link_args = []

# 源文件列表
source_files = [
    "src/pybind11_wrapper.cpp",
    "src/module_optimizer.cpp",
    "src/module_optimizer_opencl.cpp",
]

# 库和包含目录
libraries = []
library_dirs = []
include_dirs = [pybind11.get_include()]

if use_cuda:
    print("🚀 启用CUDA支持")
    if compile_cuda_code(cuda_home):
        extra_compile_args.append("/DUSE_CUDA" if is_windows else "-DUSE_CUDA")
        include_dirs.extend([
            f"{cuda_home}\\include",
            "src"
        ])
        libraries.extend(['cudart_static', 'cuda'])
        library_dirs.extend([f"{cuda_home}\\lib\\x64"])
        extra_link_args.append("src/module_optimizer_cuda.obj")
        print("✅ CUDA配置完成")
    else:
        print("⚠️ CUDA编译失败, 回退到CPU版本")
        use_cuda = False
else:
    print("⚠️ 未检测到CUDA, 使用CPU版本")

# 可选启用 OpenCL
force_no_opencl = os.environ.get('FORCE_NO_OPENCL') == '1' or force_cpu
if force_no_opencl:
    use_opencl = False
    print("禁用OpenCL支持")
else:
    opencl_conf = find_opencl()
    use_opencl = opencl_conf is not None
    if use_opencl:
        if is_windows:
            extra_compile_args.append("/DUSE_OPENCL")
        else:
            extra_compile_args.append("-DUSE_OPENCL")
        include_dirs.append(opencl_conf['include'])
        library_dirs.append(opencl_conf['libdir'])
        libraries.append('OpenCL')
        print("✅ 启用OpenCL支持")
    else:
        print("未启用OpenCL(未找到构建依赖), 将仅提供CUDA/CPU")

# 定义扩展模块
ext_modules = [
    Pybind11Extension(
        "module_optimizer_cpp",
        source_files,
        include_dirs=include_dirs,
        libraries=libraries,
        library_dirs=library_dirs,
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
        language='c++'
    ),
]

setup(
    name="module_optimizer_cpp",
    version="1.4.0",
    author="StarResonanceAutoMod",
    description="C++ implementation with CUDA GPU acceleration for module optimizer",
    long_description="High-performance C++ extension with CUDA GPU acceleration for module optimization algorithms",
    ext_modules=ext_modules,
    zip_safe=False,
    python_requires=">=3.8",
    install_requires=[
        "pybind11>=2.10.0",
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: C++",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
)
