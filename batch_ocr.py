#!/usr/bin/env python3
"""Batch OCR scanned-book PDFs with preprocessing and image-only compression.

Pipeline for each PDF:
    render pages -> deskew -> denoise -> binarize -> image-only PDF
    -> OCRmyPDF/Tesseract -> optional Ghostscript image compression

The optional compression pass is accepted only when the page count is unchanged,
the OCR text remains extractable, and the resulting file is smaller.
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


LOG = logging.getLogger("batch-ocr")
MB = 1_000_000
VERSION = "0.1.0"


class ProcessingError(RuntimeError):
    """An expected, user-actionable processing failure."""


@dataclass(frozen=True)
class Config:
    input_dir: Path
    output_dir: Path
    recursive: bool
    overwrite: bool
    dpi: int
    language: str
    jobs: int
    preprocess_jobs: int
    auto_rotate: bool
    deskew_max_angle: float
    deskew_min_angle: float
    denoise: str
    denoise_strength: float
    binarization: str
    adaptive_block_size: int
    adaptive_c: int
    compression_threshold_mb: float
    compression_dpi: int
    jpeg_quality: int
    output_suffix: str
    keep_temp: bool


@dataclass
class Result:
    source: Path
    output: Path | None
    status: str
    elapsed_seconds: float
    compressed: bool = False
    before_bytes: int = 0
    after_bytes: int = 0
    message: str = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量预处理并 OCR 影印 PDF；超过阈值时仅重采样/压缩图像。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input_dir", type=Path, help="包含 PDF 的输入文件夹")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("-o", "--output-dir", type=Path, help="输出文件夹；默认位于输入目录下的 ocr_output")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描子文件夹")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")

    ocr = parser.add_argument_group("OCR")
    ocr.add_argument("--language", default="chi_sim+eng", help="Tesseract 语言组合")
    ocr.add_argument("--dpi", type=int, default=300, help="页面栅格化分辨率")
    ocr.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="OCRmyPDF 并行任务数")
    ocr.add_argument(
        "--auto-rotate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="让 Tesseract 自动纠正 90/180/270 度方向",
    )

    prep = parser.add_argument_group("图像预处理")
    prep.add_argument(
        "--preprocess-jobs",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="预处理并行进程数；内存不足时设为 1",
    )
    prep.add_argument("--deskew-max-angle", type=float, default=12.0, help="自动纠偏允许的最大绝对角度")
    prep.add_argument("--deskew-min-angle", type=float, default=0.10, help="小于此角度时不旋转")
    prep.add_argument("--denoise", choices=("median", "nlm", "none"), default="median", help="去噪方式")
    prep.add_argument("--denoise-strength", type=float, default=7.0, help="NLM 去噪强度；median 模式忽略")
    prep.add_argument(
        "--binarization",
        choices=("adaptive", "otsu", "none"),
        default="adaptive",
        help="二值化方式；含大量照片时可用 none",
    )
    prep.add_argument("--adaptive-block-size", type=int, default=41, help="自适应二值化邻域（奇数）")
    prep.add_argument("--adaptive-c", type=int, default=15, help="自适应二值化常数，越大背景越白")

    comp = parser.add_argument_group("条件压缩")
    comp.add_argument("--compression-threshold-mb", type=float, default=50.0, help="OCR 成品超过此大小才压缩；0 表示禁用")
    comp.add_argument("--compression-dpi", type=int, default=200, help="压缩时彩色、灰度和单色图像目标 DPI")
    comp.add_argument("--jpeg-quality", type=int, default=72, help="彩色/灰度图像 JPEG 品质（1-100）")

    misc = parser.add_argument_group("其他")
    misc.add_argument("--output-suffix", default="_ocr", help="输出文件名后缀")
    misc.add_argument("--keep-temp", action="store_true", help="保留中间文件，便于诊断")
    misc.add_argument("-v", "--verbose", action="store_true", help="显示详细日志")
    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Config:
    input_dir = args.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"输入文件夹不存在：{input_dir}")
    output_dir = (args.output_dir or input_dir / "ocr_output").expanduser().resolve()

    if not 150 <= args.dpi <= 600:
        parser.error("--dpi 应在 150 到 600 之间")
    if not 0 <= args.deskew_min_angle <= args.deskew_max_angle <= 45:
        parser.error("纠偏角度应满足 0 <= min <= max <= 45")
    if args.adaptive_block_size < 3 or args.adaptive_block_size % 2 == 0:
        parser.error("--adaptive-block-size 必须是大于等于 3 的奇数")
    if args.denoise_strength < 0:
        parser.error("--denoise-strength 不能为负数")
    if args.compression_threshold_mb < 0:
        parser.error("--compression-threshold-mb 不能为负数")
    if not 72 <= args.compression_dpi <= args.dpi:
        parser.error("--compression-dpi 应在 72 与 --dpi 之间")
    if not 1 <= args.jpeg_quality <= 100:
        parser.error("--jpeg-quality 应在 1 到 100 之间")
    if args.jobs < 1:
        parser.error("--jobs 至少为 1")
    if args.preprocess_jobs < 1:
        parser.error("--preprocess-jobs 至少为 1")
    if not args.language.strip():
        parser.error("--language 不能为空")
    if not args.output_suffix or any(c in args.output_suffix for c in '<>:"/\\|?*'):
        parser.error("--output-suffix 为空或包含文件名非法字符")

    return Config(
        input_dir=input_dir,
        output_dir=output_dir,
        recursive=args.recursive,
        overwrite=args.overwrite,
        dpi=args.dpi,
        language=args.language,
        jobs=args.jobs,
        preprocess_jobs=args.preprocess_jobs,
        auto_rotate=args.auto_rotate,
        deskew_max_angle=args.deskew_max_angle,
        deskew_min_angle=args.deskew_min_angle,
        denoise=args.denoise,
        denoise_strength=args.denoise_strength,
        binarization=args.binarization,
        adaptive_block_size=args.adaptive_block_size,
        adaptive_c=args.adaptive_c,
        compression_threshold_mb=args.compression_threshold_mb,
        compression_dpi=args.compression_dpi,
        jpeg_quality=args.jpeg_quality,
        output_suffix=args.output_suffix,
        keep_temp=args.keep_temp,
    )


def missing_python_packages() -> list[str]:
    modules = {
        "cv2": "opencv-python-headless",
        "img2pdf": "img2pdf",
        "numpy": "numpy",
        "PIL": "Pillow",
        "pypdf": "pypdf",
        "pypdfium2": "pypdfium2",
        "ocrmypdf": "ocrmypdf",
    }
    return [package for module, package in modules.items() if importlib.util.find_spec(module) is None]


def preflight(config: Config) -> None:
    missing = missing_python_packages()
    if missing:
        packages = " ".join(missing)
        raise ProcessingError(
            "缺少 Python 依赖："
            f"{', '.join(missing)}\n请先运行：python -m pip install {packages}"
        )

    tesseract = find_tesseract()
    if not tesseract:
        raise ProcessingError("未找到 tesseract。请安装 Tesseract，并将其加入 PATH。")

    completed = subprocess.run(
        [tesseract, "--list-langs"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    installed = {line.strip() for line in completed.stdout.splitlines()[1:] if line.strip()}
    requested = {name.strip() for name in config.language.split("+") if name.strip()}
    unavailable = sorted(requested - installed)
    if unavailable:
        raise ProcessingError(
            "Tesseract 缺少语言数据："
            f"{', '.join(unavailable)}。当前请求为 {config.language}。"
        )


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def discover_pdfs(config: Config) -> list[Path]:
    iterator: Iterable[Path]
    iterator = config.input_dir.rglob("*.pdf") if config.recursive else config.input_dir.glob("*.pdf")
    files = []
    for path in iterator:
        resolved = path.resolve()
        if is_relative_to(resolved, config.output_dir):
            continue
        if path.stem.endswith(config.output_suffix):
            continue
        files.append(resolved)
    return sorted(files, key=lambda item: str(item).casefold())


def output_path_for(source: Path, config: Config) -> Path:
    if config.recursive:
        relative_parent = source.parent.relative_to(config.input_dir)
        parent = config.output_dir / relative_parent
    else:
        parent = config.output_dir
    return parent / f"{source.stem}{config.output_suffix}.pdf"


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    pairs = sorted(zip(values, weights), key=lambda pair: pair[0])
    midpoint = sum(weights) / 2
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= midpoint:
            return value
    return pairs[-1][0]


def angle_from_nearest_text_axis(angle: float) -> float:
    """Return skew relative to horizontal or vertical text flow."""
    if angle > 45:
        return angle - 90
    if angle < -45:
        return angle + 90
    return angle


def estimate_skew_angle(gray, max_angle: float) -> float:
    """Estimate small page skew using weighted horizontal/vertical segments."""
    import cv2
    import numpy as np

    height, width = gray.shape[:2]
    scale = min(1.0, 1800.0 / max(height, width))
    sample = gray
    if scale < 1.0:
        sample = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    blurred = cv2.GaussianBlur(sample, (3, 3), 0)
    _, ink = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel_width = max(15, sample.shape[1] // 60)
    kernel_height = max(15, sample.shape[0] // 60)
    horizontal = cv2.morphologyEx(
        ink,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 1)),
    )
    vertical = cv2.morphologyEx(
        ink,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, kernel_height)),
    )
    text_structure = cv2.bitwise_or(horizontal, vertical)
    edges = cv2.Canny(text_structure, 50, 150, apertureSize=3)
    shorter_side = min(sample.shape[:2])
    min_length = max(30, shorter_side // 8)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 1800,
        threshold=max(30, shorter_side // 25),
        minLineLength=min_length,
        maxLineGap=max(10, shorter_side // 50),
    )
    if lines is None:
        return 0.0

    angles: list[float] = []
    lengths: list[float] = []
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
        while angle <= -90:
            angle += 180
        while angle > 90:
            angle -= 180
        deviation = angle_from_nearest_text_axis(angle)
        if abs(deviation) <= max_angle:
            angles.append(deviation)
            lengths.append(math.hypot(x2 - x1, y2 - y1))
    if not angles:
        return 0.0
    return weighted_median(angles, lengths)


def rotate_page(gray, angle: float):
    import cv2

    height, width = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    return cv2.warpAffine(
        gray,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def preprocess_page(rgb, config: Config):
    import cv2

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    angle = estimate_skew_angle(gray, config.deskew_max_angle)
    if config.deskew_min_angle <= abs(angle) <= config.deskew_max_angle:
        # Image y coordinates point downward: a +2 degree detected slope is
        # visually clockwise and needs a +2 degree OpenCV (CCW) rotation.
        gray = rotate_page(gray, angle)
    else:
        angle = 0.0

    if config.denoise == "median":
        gray = cv2.medianBlur(gray, 3)
    elif config.denoise == "nlm":
        gray = cv2.fastNlMeansDenoising(
            gray,
            None,
            h=config.denoise_strength,
            templateWindowSize=7,
            searchWindowSize=21,
        )

    if config.binarization == "adaptive":
        processed = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            config.adaptive_block_size,
            config.adaptive_c,
        )
    elif config.binarization == "otsu":
        _, processed = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        processed = gray
    return processed, angle


def write_png(path: Path, image, bilevel: bool) -> None:
    import cv2
    from PIL import Image

    if bilevel:
        # OpenCV's threshold result is still an 8-bit image. Converting it to
        # a true 1-bit PNG allows img2pdf to use compact bilevel storage.
        Image.fromarray(image).convert("1").save(path, format="PNG", optimize=True)
        return

    success, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not success:
        raise ProcessingError(f"无法编码页面图像：{path}")
    encoded.tofile(str(path))


def render_and_preprocess_page(document, index: int, pages_dir: Path, config: Config) -> tuple[int, Path, float]:
    """Render and preprocess one PDF page using a process-local document."""
    import numpy as np

    page = document[index]
    bitmap = None
    try:
        bitmap = page.render(scale=config.dpi / 72.0)
        rgb = np.asarray(bitmap.to_pil().convert("RGB"))
        processed, angle = preprocess_page(rgb, config)
        image_path = pages_dir / f"page-{index + 1:06d}.png"
        write_png(image_path, processed, config.binarization != "none")
        return index, image_path, angle
    finally:
        if bitmap is not None:
            bitmap.close()
        page.close()


_PREPROCESS_DOCUMENT = None
_PREPROCESS_CONFIG: Config | None = None
_PREPROCESS_PAGES_DIR: Path | None = None


def close_preprocess_worker() -> None:
    global _PREPROCESS_DOCUMENT
    if _PREPROCESS_DOCUMENT is not None:
        _PREPROCESS_DOCUMENT.close()
        _PREPROCESS_DOCUMENT = None


def init_preprocess_worker(source: str, pages_dir: str, config: Config) -> None:
    """Open one independent PDFium document in each worker process."""
    import cv2
    import pypdfium2 as pdfium

    global _PREPROCESS_DOCUMENT, _PREPROCESS_CONFIG, _PREPROCESS_PAGES_DIR
    # Avoid multiplying OpenCV's internal thread pool by the process count.
    cv2.setNumThreads(1)
    _PREPROCESS_DOCUMENT = pdfium.PdfDocument(source)
    _PREPROCESS_CONFIG = config
    _PREPROCESS_PAGES_DIR = Path(pages_dir)
    atexit.register(close_preprocess_worker)


def preprocess_page_worker(index: int) -> tuple[int, str, float]:
    if _PREPROCESS_DOCUMENT is None or _PREPROCESS_CONFIG is None or _PREPROCESS_PAGES_DIR is None:
        raise RuntimeError("预处理 worker 尚未初始化")
    page_index, image_path, angle = render_and_preprocess_page(
        _PREPROCESS_DOCUMENT,
        index,
        _PREPROCESS_PAGES_DIR,
        _PREPROCESS_CONFIG,
    )
    return page_index, str(image_path), angle


def log_preprocessed_page(index: int, page_count: int, angle: float) -> None:
    LOG.info(
        "    预处理 %d/%d（纠偏角 %.2f°）",
        index + 1,
        page_count,
        angle,
    )


def build_preprocessed_pdf(source: Path, destination: Path, pages_dir: Path, config: Config) -> int:
    import img2pdf
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(source))
    except Exception as exc:
        raise ProcessingError(f"无法打开 PDF：{exc}") from exc

    page_count = len(document)
    if page_count == 0:
        document.close()
        raise ProcessingError("PDF 没有页面")

    image_paths: list[Path | None] = [None] * page_count
    worker_count = min(config.preprocess_jobs, page_count)
    LOG.info("    预处理并行进程：%d", worker_count)

    if worker_count == 1:
        try:
            for index in range(page_count):
                page_index, image_path, angle = render_and_preprocess_page(document, index, pages_dir, config)
                image_paths[page_index] = image_path
                log_preprocessed_page(page_index, page_count, angle)
        finally:
            document.close()
    else:
        document.close()
        executor = ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=init_preprocess_worker,
            initargs=(str(source), str(pages_dir), config),
        )
        futures = {executor.submit(preprocess_page_worker, index): index for index in range(page_count)}
        try:
            for future in as_completed(futures):
                index = futures[future]
                try:
                    page_index, image_path, angle = future.result()
                except Exception as exc:
                    raise ProcessingError(f"预处理第 {index + 1} 页失败：{exc}") from exc
                image_paths[page_index] = Path(image_path)
                log_preprocessed_page(page_index, page_count, angle)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    if any(path is None for path in image_paths):
        raise ProcessingError("预处理完成后发现页面图像缺失")

    ordered_paths = [path for path in image_paths if path is not None]
    try:
        with destination.open("wb") as stream:
            img2pdf.convert(
                [str(path) for path in ordered_paths],
                outputstream=stream,
                layout_fun=img2pdf.get_fixed_dpi_layout_fun((config.dpi, config.dpi)),
            )
    except Exception as exc:
        raise ProcessingError(f"无法组装预处理 PDF：{exc}") from exc
    return page_count


def run_ocr(source: Path, destination: Path, config: Config) -> None:
    command = [
        sys.executable,
        "-m",
        "ocrmypdf",
        "--language",
        config.language,
        "--output-type",
        "pdf",
        "--optimize",
        "0",
        "--jobs",
        str(config.jobs),
    ]
    if config.auto_rotate:
        command.append("--rotate-pages")
    command.extend((str(source), str(destination)))
    LOG.info("    运行 OCR（语言：%s）", config.language)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0 or not destination.is_file():
        raise ProcessingError(f"OCRmyPDF 执行失败（退出码 {completed.returncode}）")


def find_tesseract() -> str | None:
    executable = shutil.which("tesseract")
    if executable:
        return executable
    if os.name == "nt":
        candidates = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Tesseract-OCR"
            / "tesseract.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def find_ghostscript() -> str | None:
    for name in ("gswin64c", "gswin32c", "gs"):
        executable = shutil.which(name)
        if executable:
            return executable
    if os.name == "nt":
        conda_prefix = os.environ.get("CONDA_PREFIX")
        if conda_prefix:
            conda_bin = Path(conda_prefix) / "Library" / "bin"
            for name in ("gswin64c.exe", "gswin32c.exe"):
                candidate = conda_bin / name
                if candidate.is_file():
                    return str(candidate)
        roots = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "gs",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "gs",
        )
        candidates: list[Path] = []
        for root in roots:
            if root.is_dir():
                candidates.extend(root.glob("gs*/bin/gswin64c.exe"))
                candidates.extend(root.glob("gs*/bin/gswin32c.exe"))
        if candidates:
            return str(sorted(candidates, reverse=True)[0])
    return None


def pdf_text_signature(path: Path) -> tuple[int, tuple[str, ...]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    text_by_page = tuple("".join((page.extract_text() or "").split()) for page in reader.pages)
    return len(reader.pages), text_by_page


def verify_compressed_pdf(original: Path, candidate: Path) -> tuple[bool, str]:
    if not candidate.is_file() or candidate.stat().st_size == 0:
        return False, "压缩器没有生成有效文件"
    if candidate.stat().st_size >= original.stat().st_size:
        return False, "压缩后文件没有变小"
    try:
        old_pages, old_text = pdf_text_signature(original)
        new_pages, new_text = pdf_text_signature(candidate)
    except Exception as exc:
        return False, f"无法校验压缩 PDF：{exc}"
    if new_pages != old_pages:
        return False, f"压缩后页数变化（{old_pages} -> {new_pages}）"
    old_chars = sum(map(len, old_text))
    new_chars = sum(map(len, new_text))
    if new_text != old_text:
        return False, f"压缩前后文字层内容不同（{old_chars} -> {new_chars} 字符）"
    return True, f"页数 {new_pages}，文字层内容完全一致（{new_chars} 字符）"


def compress_images_only(source: Path, destination: Path, config: Config) -> None:
    ghostscript = find_ghostscript()
    if not ghostscript:
        raise ProcessingError("文件超过压缩阈值，但未找到 Ghostscript（gswin64c/gs）。")

    dpi = config.compression_dpi
    command = [
        ghostscript,
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dPreserveAnnots=true",
        "-dDownsampleColorImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        "-dColorImageDownsampleThreshold=1.0",
        f"-dColorImageResolution={dpi}",
        "-dAutoFilterColorImages=false",
        "-sColorImageFilter=DCTEncode",
        "-dDownsampleGrayImages=true",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleThreshold=1.0",
        f"-dGrayImageResolution={dpi}",
        "-dAutoFilterGrayImages=false",
        "-sGrayImageFilter=DCTEncode",
        "-dDownsampleMonoImages=true",
        "-dMonoImageDownsampleType=/Subsample",
        "-dMonoImageDownsampleThreshold=1.0",
        f"-dMonoImageResolution={dpi}",
        "-sMonoImageFilter=CCITTFaxEncode",
        f"-dJPEGQ={config.jpeg_quality}",
        f"-sOutputFile={destination}",
        str(source),
    ]
    LOG.info(
        "    文件超过 %.1f MB，压缩图像（%d DPI，JPEG 品质 %d）",
        config.compression_threshold_mb,
        dpi,
        config.jpeg_quality,
    )
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout or "").strip()
        if diagnostic:
            diagnostic = diagnostic.splitlines()[-1]
            raise ProcessingError(
                f"Ghostscript 压缩失败（退出码 {completed.returncode}）：{diagnostic}"
            )
        raise ProcessingError(f"Ghostscript 压缩失败（退出码 {completed.returncode}）")


def safe_temp_directory(source: Path, keep: bool) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if keep:
        path = source.parent / f".{source.stem}.ocr-temp-{int(time.time())}"
        path.mkdir(parents=True, exist_ok=False)
        return path, None
    manager = tempfile.TemporaryDirectory(prefix="pdf-batch-ocr-")
    return Path(manager.name), manager


def process_one(source: Path, output: Path, config: Config) -> Result:
    started = time.monotonic()
    if output.exists() and not config.overwrite:
        return Result(source, output, "skipped", 0.0, message="输出已存在")

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_dir, temp_manager = safe_temp_directory(output, config.keep_temp)
    pages_dir = temp_dir / "pages"
    pages_dir.mkdir()
    preprocessed = temp_dir / "preprocessed.pdf"
    ocr_result = temp_dir / "ocr.pdf"
    compressed_result = temp_dir / "compressed.pdf"
    compressed = False
    warning = ""
    before_bytes = 0
    after_bytes = 0
    try:
        LOG.info("处理：%s", source)
        page_count = build_preprocessed_pdf(source, preprocessed, pages_dir, config)
        LOG.info("    已生成 %d 页预处理 PDF", page_count)
        run_ocr(preprocessed, ocr_result, config)
        ocr_pages, ocr_text = pdf_text_signature(ocr_result)
        if ocr_pages != page_count:
            raise ProcessingError(f"OCR 后页数变化（{page_count} -> {ocr_pages}）")
        recognized_chars = sum(map(len, ocr_text))
        if recognized_chars == 0:
            warning = "OCR 完成，但未提取到任何文字；请检查语言包和页面质量"
            LOG.warning("    %s", warning)
        else:
            LOG.info("    OCR 校验通过：%d 页，%d 个非空白字符", ocr_pages, recognized_chars)
        before_bytes = ocr_result.stat().st_size
        selected = ocr_result

        threshold_bytes = int(config.compression_threshold_mb * MB)
        if threshold_bytes > 0 and before_bytes > threshold_bytes:
            try:
                compress_images_only(ocr_result, compressed_result, config)
                valid, detail = verify_compressed_pdf(ocr_result, compressed_result)
                if valid:
                    selected = compressed_result
                    compressed = True
                    LOG.info("    压缩校验通过：%s", detail)
                else:
                    warning = f"压缩校验未通过：{detail}；已保留原 OCR 文件"
                    LOG.warning("    %s", warning)
            except ProcessingError as exc:
                # OCR succeeded, so a missing/failed optional compressor should not
                # discard the useful searchable PDF.
                warning = f"压缩未完成：{exc}；已保留原 OCR 文件"
                LOG.warning("    %s", warning)

        after_bytes = selected.stat().st_size
        if compressed and after_bytes > threshold_bytes:
            warning = (
                f"压缩后仍为 {format_mb(after_bytes)}，高于阈值；"
                "可降低 --compression-dpi 或 --jpeg-quality"
            )
            LOG.warning("    %s", warning)
        staging = output.with_name(f".{output.name}.partial")
        if staging.exists():
            staging.unlink()
        shutil.copyfile(selected, staging)
        os.replace(staging, output)
        return Result(
            source=source,
            output=output,
            status="ok",
            elapsed_seconds=time.monotonic() - started,
            compressed=compressed,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            message=warning,
        )
    except Exception as exc:
        return Result(
            source=source,
            output=None,
            status="failed",
            elapsed_seconds=time.monotonic() - started,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            message=str(exc),
        )
    finally:
        if temp_manager is not None:
            temp_manager.cleanup()
        else:
            LOG.info("    中间文件保留于：%s", temp_dir)


def format_mb(size: int) -> str:
    return f"{size / MB:.1f} MB"


def print_summary(results: Sequence[Result]) -> None:
    ok = sum(result.status == "ok" for result in results)
    skipped = sum(result.status == "skipped" for result in results)
    failed = sum(result.status == "failed" for result in results)
    print("\n处理结果")
    print(f"  成功：{ok}，跳过：{skipped}，失败：{failed}")
    for result in results:
        if result.status == "ok":
            compression = ""
            if result.compressed:
                compression = f"，已压缩 {format_mb(result.before_bytes)} -> {format_mb(result.after_bytes)}"
            label = "成功/警告" if result.message else "成功"
            warning = f"；{result.message}" if result.message else ""
            print(f"  [{label}] {result.source.name} -> {result.output}{compression}{warning}")
        elif result.status == "skipped":
            print(f"  [跳过] {result.source.name}：{result.message}")
        else:
            print(f"  [失败] {result.source.name}：{result.message}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    config = validate_args(args, parser)
    try:
        preflight(config)
    except ProcessingError as exc:
        parser.error(str(exc))

    sources = discover_pdfs(config)
    if not sources:
        LOG.error("未在 %s 找到待处理 PDF", config.input_dir)
        return 2
    LOG.info("找到 %d 个 PDF，输出目录：%s", len(sources), config.output_dir)

    results = [process_one(source, output_path_for(source, config), config) for source in sources]
    print_summary(results)
    return 1 if any(result.status == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
