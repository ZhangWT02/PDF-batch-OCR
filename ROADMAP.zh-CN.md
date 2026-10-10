# 开发路线图

[English](ROADMAP.md)

这份路线图记录 PDF Batch OCR 下一阶段在易用性和项目传播方面的工作。暂不承诺具体日期，直到打包方案和后续维护成本得到验证。每项工作应使用独立的 GitHub Issue 跟踪。

## 近期：项目演示 GIF（[#1](https://github.com/ZhangWT02/PDF-batch-OCR/issues/1)）

为中英文 README 制作一段 15～30 秒的动态演示。

完成标准：

- 使用自行制作或允许再分发的小型示例 PDF；
- 准确描述输入文件为“未内嵌可搜索文字层的 PDF”；
- 展示一条批处理命令、经过剪辑的处理进度，以及在输出 PDF 中成功搜索关键词；
- 不暴露私人路径、文档或个人信息；
- 在 README 显示宽度下保持文字清晰，并控制文件大小。

## v0.2.0：Windows 一键安装（[#2](https://github.com/ZhangWT02/PDF-batch-OCR/issues/2)）

为 Windows 用户提供受支持的安装方式，不再要求用户手动搭建 Python 环境。

完成标准：

- 能够在全新的受支持 Windows 系统上安装 `pdf-batch-ocr` 命令；
- 检测或明确处理 Tesseract、所需语言数据、Ghostscript 及其他运行时依赖；
- 缺少依赖时给出可操作的诊断和修复建议；
- 支持卸载和升级，且不删除用户文档及 OCR 输出；
- 捆绑第三方程序前，记录其许可证并确认再分发限制；
- 通过 GitHub Release 发布安装程序和校验值。

正式实现前，应先在对应 Issue 中确定打包方案。将第三方可执行文件直接打包，与安装时下载依赖，在维护、安全和许可证方面有不同影响。

## v0.3.0：桌面可视化界面（[#3](https://github.com/ZhangWT02/PDF-batch-OCR/issues/3)）

为不希望使用终端的用户提供简洁的桌面界面。命令行仍然受到支持；GUI 应复用同一套核心逻辑，避免维护两套 OCR 流程。

首个版本的最小范围：

- 选择输入和输出文件夹；
- 设置递归扫描、OCR 语言、预处理方案和并行数；
- 显示文件队列、当前阶段、处理进度和有用的错误信息；
- 能够安全取消正在运行的批处理；
- 处理完成后打开输出文件夹；
- 保留现有的跳过、覆盖、校验和单文件失败隔离行为。

首个 GUI 版本暂不包含：

- 编辑单个 PDF 页面；
- 手工校正 OCR 文字；
- 云端 OCR 或账号功能。

## GitHub 跟踪方式

每项工作建立一个独立 Issue，并放入对应 Milestone：

1. [Add an animated README demo](https://github.com/ZhangWT02/PDF-batch-OCR/issues/1) → `v0.1.x`
2. [Provide a one-click Windows installer](https://github.com/ZhangWT02/PDF-batch-OCR/issues/2) → `v0.2.0`
3. [Build a desktop GUI for batch OCR](https://github.com/ZhangWT02/PDF-batch-OCR/issues/3) → `v0.3.0`

等相关 Issue 较多、确实需要跨版本排期时，再启用 GitHub Project 看板。目前只有这三项，使用 Issues 和 Milestones 已经足够。
