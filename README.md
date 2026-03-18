# StarResonanceAutoModGUI

StarResonanceAutoModGUI 是基于开源项目 [StarResonanceAutoMod](https://github.com/fudiyangjin/StarResonanceAutoMod) 修改而来的图形界面版本。

本项目主要面向希望通过图形界面使用相关功能的用户，在保留原项目核心能力的基础上，对启动方式、使用流程与部分构建体验进行了适配与整理。

---

## 项目信息

- 项目名称：StarResonanceAutoModGUI
- 当前版本：v1.5-alpha
- 版本代号：Sonnenschein
- 原项目名称：StarResonanceAutoMod
- 原项目作者：fudiyangjin
- 原项目地址：https://github.com/fudiyangjin/StarResonanceAutoMod
- 开源协议：AGPL-3.0

---

## 项目说明

本项目为基于原项目进行修改、整理与扩展后的 GUI 版本。

当前版本主要目标包括：

- 为原项目提供图形界面入口
- 对部分构建与检测流程进行调整

---

## 与原项目的主要差异

当前版本相较于原项目，主要改动包括：

- 新增图形界面启动入口，便于普通用户使用
- 调整部分 CUDA / C++ 扩展相关的检测与构建逻辑

---

## 使用说明

### 1. 获取项目

将本仓库完整下载或克隆到本地。

### 2. 运行方式

根据项目提供的 GUI 启动入口运行程序。

编译 C++扩展

CPU 版本编译：

```powershell
cd cpp_extension
python setup.py build_ext --inplace
cd ..
```

若当前版本包含 `gui_main.py`，可优先使用该入口启动图形界面。

示例：

```powershell
python gui_main.py
```

若你的环境已经完成依赖安装与扩展构建，可直接通过图形界面进行后续操作。

---

## 环境说明

建议使用可正常运行原项目的 Python 环境。

如需启用部分加速能力或扩展模块，可能还需要：

- Python 运行环境
- 对应依赖库
- Visual Studio C++ 编译环境
- CUDA 工具链（如需启用 CUDA 相关功能）

若不使用相关扩展能力，则可按当前版本实际情况仅使用图形界面与基础功能。

---

## 版本说明

当前发布版本为：

**v1.5-alpha / Sonnenschein**

这是一个早期 alpha 版本，可能仍存在：

- 功能尚未完全稳定
- 某些环境兼容性差异
- 部分说明仍会在后续继续补充

因此更适合作为早期测试版本使用。

---

## 致谢

本项目基于开源项目 [StarResonanceAutoMod](https://github.com/fudiyangjin/StarResonanceAutoMod) 进行修改与扩展。

感谢原作者 **fudiyangjin** 提供原始项目与核心实现。  
本项目的 GUI 化整理与相关适配，均建立在原项目已公开的代码与结构基础之上。

若你使用、修改或再分发本项目，也请继续保留对原项目及原作者的归属说明。

---

## 许可证

本项目遵循 **AGPL-3.0** 协议发布。

这意味着：

- 你可以在遵守协议前提下使用、修改和再分发本项目
- 你在分发修改版本时，应保留原有许可与归属说明
- 若你基于本项目继续修改并对外提供，也应遵守 AGPL-3.0 的相应要求

详细协议内容请参见仓库根目录下的 `LICENSE` 文件。

---

## 原项目链接

- 原项目仓库：https://github.com/fudiyangjin/StarResonanceAutoMod
