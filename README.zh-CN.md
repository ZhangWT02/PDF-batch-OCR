<div align="center">

# PDF Batch OCR

**面向扫描版书籍的本地批量多语言 OCR 工具**

[English](README.md) · **简体中文**

</div>

本工具在本地批量处理扫描版书籍 PDF，识别语言可通过 `--language` 配置，并提供图像预处理、受控并行和文字层安全校验。

> A local-first batch OCR pipeline for scanned books, with configurable Tesseract languages,
> image preprocessing, controlled parallelism, and text-layer-safe compression.

所有文档均在本机处理，程序不会上传 PDF 或 OCR 内容。请仅处理你有权使用的文档。

## 功能

- 自动发现文件夹中的 PDF，可选择递归扫描；
- 300 DPI 页面渲染、小角度纠偏和去噪，默认保留彩色并使用 JPEG 编码；
- 可对纯文字扫描件显式启用自适应或 Otsu 二值化，生成紧凑的 1-bit 黑白页面；
- 最多 4 个独立进程并行预处理，并保持原始页序；
- 可通过 `--language` 使用任意已安装的 Tesseract 语言或语言组合，默认值为简体中文和英文（`chi_sim+eng`）；
- 生成不可见、可搜索的 OCR 文字层；
- 默认同时生成经过分页、段落合并和英文断词修复的 Markdown 文本；
- 成品超过 50 MB 时仅压缩页面图像；
- 压缩前后逐页核对文字内容，校验失败时保留未压缩版本；
- 单个文件失败不会中断整批任务。

## 处理流程

```mermaid
flowchart LR
    A[扫描 PDF] --> B[逐页渲染]
    B --> C[纠偏 / 去噪 / 可选二值化]
    C --> D[按原页序组装]
    D --> E[OCRmyPDF + Tesseract]
    E --> M[词坐标与栏间空白分析为 Markdown]
    E --> F{超过大小阈值?}
    F -- 否 --> G[输出可搜索 PDF]
    F -- 是 --> H[仅压缩图像]
    H --> I{页数与文字层一致?}
    I -- 是 --> G
    I -- 否 --> J[回退到未压缩 OCR 文件]
```

默认流程是：

1. 以 300 DPI 逐页渲染，自动估计并纠正小角度倾斜；
2. 中值去噪；
3. 默认保留 RGB 彩色，以 JPEG 品质 72 编码页面；选择 `adaptive` 或 `otsu` 时改为 1-bit 黑白 PNG；
4. 使用 OCRmyPDF + Tesseract 写入不可见、可搜索的文字层；
5. 对预处理页执行一次结构化 Tesseract OCR，根据词坐标和栏间空白恢复阅读顺序，再生成 Markdown；
6. OCR 成品超过 50 MB（50,000,000 字节）时，使用 Ghostscript 只对页面图像降采样/压缩；
7. 比较压缩前后的页数和可提取文字量。校验失败时自动保留未压缩的 OCR 文件。

识别语言可通过 `--language` 配置，支持 Tesseract 中已安装的任意语言数据。默认值为简体中文和英文：`chi_sim+eng`。

## 快速开始

需要 Python 3.11+，以及系统程序：

- Tesseract OCR，并安装所选语言需要的语言数据（默认配置需要 `chi_sim` 和 `eng`）；
- Ghostscript（超阈值的图像压缩所需）；
- 推荐安装 Noto CJK 字体，供新版 OCRmyPDF 正确构造中日韩文字层。

Windows 的 Tesseract、Ghostscript 与 OCRmyPDF 安装方式可参考
[OCRmyPDF 官方安装说明](https://ocrmypdf.readthedocs.io/en/latest/installation.html#native-windows)。

### Conda（推荐）

```powershell
conda env create -f environment.yml
conda activate pdf-batch-ocr
```

部分 Windows Conda 构建会把 Tesseract 语言数据和输出配置装到不同目录。如果运行时出现
`Can't open hocr`，执行一次：

```powershell
.\scripts\repair_conda_tessdata.ps1
```

检查环境：

```powershell
python -m pip check
python -m ocrmypdf --version
tesseract --list-langs
gswin64c -version
```

### venv + pip

确认命令行能够找到它们：

```powershell
tesseract --version
tesseract --list-langs
gswin64c -version
```

创建虚拟环境并安装 Python 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

也可以把项目安装为命令行工具：

```powershell
python -m pip install -e .
pdf-batch-ocr "D:\Books\待处理"
```

## 使用

处理一个文件夹里的全部 PDF：

```powershell
python .\batch_ocr.py "D:\Books\待处理"
```

预处理默认最多使用 4 个独立进程。可根据 CPU 和内存调整：

```powershell
python .\batch_ocr.py "D:\Books\待处理" --preprocess-jobs 4
```

内存不足或需要恢复串行处理时：

```powershell
python .\batch_ocr.py "D:\Books\待处理" --preprocess-jobs 1
```

结果默认写入 `D:\Books\待处理\ocr_output`，每本书同时生成 `书名_ocr.pdf` 和 `书名_ocr.md`。如果 PDF 已存在但 Markdown 缺失，脚本会直接从现有文字层补生成 Markdown；两者均存在时会跳过。确认需要重做可加 `--overwrite`。

指定输出目录并递归处理：

```powershell
python .\batch_ocr.py "D:\Books\待处理" -o "D:\Books\OCR完成" --recursive
```

调整 JPEG 品质、成品阈值和后续图像压缩参数：

```powershell
python .\batch_ocr.py "D:\Books\待处理" `
  --compression-threshold-mb 50 `
  --compression-dpi 200 `
  --jpeg-quality 72
```

纯文字扫描件需要更小体积和更干净背景时，可启用自适应二值化：

```powershell
python .\batch_ocr.py "D:\Books\待处理" --binarization adaptive
```

如只需要可搜索 PDF，可关闭 Markdown 输出：

```powershell
python .\batch_ocr.py "D:\Books\待处理" --no-markdown
```

指定任意已安装的 Tesseract 语言或语言组合，例如繁体中文和英文：

```powershell
python .\batch_ocr.py "D:\Books\待处理" --language chi_tra+eng
```

查看全部可调参数：

```powershell
python .\batch_ocr.py --help
```

## 参数建议

| 场景 | 建议 |
|---|---|
| 含照片、彩图或细腻灰阶插图 | 默认 `--binarization none`，保留彩色并使用 JPEG |
| 泛黄、明暗不均的纯文字书 | `--binarization adaptive` |
| 背景较均匀的扫描件 | `--binarization otsu` |
| 椒盐噪点 | 默认 `--denoise median` |
| 轻微连续噪声 | `--denoise nlm --denoise-strength 7` |
| 小字、古籍批注 | 可保留 `--dpi 300`，不要把压缩 DPI 调得过低 |

压缩阶段不会把整页重新栅格化：Ghostscript 只设置图像降采样和图像编码参数，文字及字体对象仍作为文字层处理。脚本还会逐页读取压缩前后的文字层；若文字内容不是完全一致，压缩结果不会被采用。

## 注意事项

- 默认 `--binarization none` 会保留 RGB 彩色，并将预处理页面编码为 JPEG；`--jpeg-quality` 同时控制该编码和超阈值后的 JPEG 压缩品质。
- `--binarization adaptive` 和 `--binarization otsu` 会把页面转为黑白，并以真正的 1-bit PNG 保存，通常更适合纯文字扫描件。
- Markdown 使用 Tesseract TSV 词坐标和栏间空白检测恢复阅读顺序；规则双栏会先输出完整左栏，再输出右栏，并会保留段落空行、合并段内换行及修复英文行末断词。
- 为获得词坐标，默认 Markdown 会额外执行一次 Tesseract 结构化识别，因此总处理时间会比仅生成 PDF 更长；可用 `--no-markdown` 关闭。
- Markdown 是内容结构副本，不会复刻 PDF 的字体和精确坐标；复杂表格、跨栏标题或非常规版式仍可能需要人工调整或专用版面分析模型。
- 自动纠偏处理的是常见的小角度歪斜；90/180/270 度方向由 OCRmyPDF 的自动旋转处理。
- `--preprocess-jobs` 控制渲染、纠偏、去噪、可选二值化和 JPEG/PNG 编码的并行进程数；`--jobs` 仅控制后续 OCR。
- 单个 PDF 失败不会中断同一批次中的其他文件；最后会打印成功、跳过和失败汇总。
- 使用 `--keep-temp` 可以保留预处理页面和中间 PDF，便于排查特殊版面。
- `books/`、所有 PDF 和 OCR 中间产物默认被 Git 忽略，避免误上传私人或受版权保护的文档。

## 开发与测试

计划中的 README 演示、Windows 一键安装程序和桌面可视化界面，请参阅[开发路线图](ROADMAP.zh-CN.md)。

项目使用 Python 标准库 `unittest`，基础测试不依赖 OCR 系统程序：

```powershell
python -m py_compile .\batch_ocr.py .\tests\test_batch_ocr.py
python -m unittest discover -s tests -v
```

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。Bug 报告中不要上传机密或受版权保护的 PDF。

## 许可证

本项目使用 [MIT License](LICENSE)。Tesseract、Ghostscript、OCRmyPDF 及其他第三方依赖分别适用其自身许可证。
