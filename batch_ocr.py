#!/usr/bin/env python3
"""Batch OCR scanned-book PDFs with preprocessing and image-only compression.

Pipeline for each PDF:
    render pages -> deskew -> denoise -> preserve color or binarize
    -> JPEG or 1-bit PNG -> image-only PDF
    -> structured Markdown only, or
    -> OCRmyPDF/Tesseract -> searchable PDF + structured Markdown
    -> optional Ghostscript image compression

The optional compression pass is accepted only when the page count is unchanged,
the OCR text remains extractable, and the resulting file is smaller.
"""

from __future__ import annotations

import argparse
import atexit
import csv
import importlib.util
import io
import logging
import math
import os
import re
import shutil
import statistics
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
WEREAD_SECTION_MAX_BYTES = 64 * 1024
VERSION = "0.1.0"
IS_WINDOWS = os.name == "nt"

PAGE_MARKER_RE = re.compile(r"(?m)^<!-- page: (\d+) -->[ \t]*$")
WEREAD_SECTION_RE = re.compile(r"(?m)^## 阅读分段 \d+[ \t]*$\n*")
FOOTNOTE_RE = re.compile(r"^(\d{1,3})[.,]\s+(.+)$")


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
    markdown: bool
    markdown_only: bool
    weread_markdown: bool
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
    markdown_output: Path | None = None
    weread_markdown_output: Path | None = None


@dataclass(frozen=True)
class PositionedWord:
    text: str
    x: float
    y: float
    font_size: float
    width: float


@dataclass
class PositionedLine:
    y: float
    words: list[PositionedWord]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量预处理并 OCR 扫描型 PDF；超过阈值时仅重采样/压缩图像。",
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
        default="none",
        help="二值化方式；默认 none 保留彩色，adaptive/otsu 输出黑白",
    )
    prep.add_argument("--adaptive-block-size", type=int, default=41, help="自适应二值化邻域（奇数）")
    prep.add_argument("--adaptive-c", type=int, default=15, help="自适应二值化常数，越大背景越白")

    comp = parser.add_argument_group("图像编码与条件压缩")
    comp.add_argument("--compression-threshold-mb", type=float, default=50.0, help="OCR 成品超过此大小才压缩；0 表示禁用")
    comp.add_argument("--compression-dpi", type=int, default=200, help="压缩时彩色、灰度和单色图像目标 DPI")
    comp.add_argument(
        "--jpeg-quality",
        type=int,
        default=72,
        help="彩色页面及条件压缩中的 JPEG 品质（1-100）",
    )

    misc = parser.add_argument_group("其他")
    misc.add_argument("--output-suffix", default="_ocr", help="输出文件名后缀")
    misc.add_argument(
        "--markdown",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="同时生成经过段落整理的 Markdown 文件",
    )
    misc.add_argument(
        "--markdown-only",
        action="store_true",
        help="只生成 Markdown；跳过 OCR PDF 与条件压缩",
    )
    misc.add_argument(
        "--weread-markdown",
        action="store_true",
        help="额外生成删除重复页眉/页码、整理脚注并按大小分段的微信读书版 Markdown",
    )
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
    if args.markdown_only and not args.markdown:
        parser.error("--markdown-only 不能与 --no-markdown 同时使用")
    if args.weread_markdown and not args.markdown:
        parser.error("--weread-markdown 不能与 --no-markdown 同时使用")

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
        markdown=args.markdown,
        markdown_only=args.markdown_only,
        weread_markdown=args.weread_markdown,
        keep_temp=args.keep_temp,
    )


def missing_python_packages(markdown_only: bool = False) -> list[str]:
    modules = {
        "cv2": "opencv-python-headless",
        "numpy": "numpy",
        "PIL": "Pillow",
        "pypdfium2": "pypdfium2",
    }
    if not markdown_only:
        modules.update(
            {
                "img2pdf": "img2pdf",
                "pypdf": "pypdf",
                "ocrmypdf": "ocrmypdf",
            }
        )
    return [package for module, package in modules.items() if importlib.util.find_spec(module) is None]


def preflight(config: Config) -> None:
    missing = missing_python_packages(config.markdown_only)
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
    iterator = config.input_dir.rglob("*") if config.recursive else config.input_dir.glob("*")
    files = []
    for path in iterator:
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
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


def markdown_path_for(output: Path) -> Path:
    return output.with_suffix(".md")


def weread_markdown_path_for(markdown_output: Path) -> Path:
    return markdown_output.with_name(f"{markdown_output.stem}_weread.md")


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


def rotate_page(image, angle: float):
    import cv2

    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    border_value = (255, 255, 255) if image.ndim == 3 else 255
    return cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_value,
    )


def preprocess_page(rgb, config: Config):
    import cv2

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    angle = estimate_skew_angle(gray, config.deskew_max_angle)
    should_rotate = config.deskew_min_angle <= abs(angle) <= config.deskew_max_angle
    if not should_rotate:
        angle = 0.0

    if config.binarization == "none":
        processed = rotate_page(rgb, angle) if should_rotate else rgb
        if config.denoise == "median":
            processed = cv2.medianBlur(processed, 3)
        elif config.denoise == "nlm":
            processed = cv2.fastNlMeansDenoisingColored(
                processed,
                None,
                h=config.denoise_strength,
                hColor=config.denoise_strength,
                templateWindowSize=7,
                searchWindowSize=21,
            )
        return processed, angle

    if should_rotate:
        # Image y coordinates point downward: a +2 degree detected slope is
        # visually clockwise and needs a +2 degree OpenCV (CCW) rotation.
        gray = rotate_page(gray, angle)

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
    return processed, angle


def page_image_suffix(config: Config) -> str:
    return ".jpg" if config.binarization == "none" else ".png"


def write_page_image(path: Path, image, bilevel: bool, jpeg_quality: int) -> None:
    from PIL import Image

    pil_image = Image.fromarray(image)
    if bilevel:
        # OpenCV's threshold result is still an 8-bit image. Converting it to
        # a true 1-bit PNG allows img2pdf to use compact bilevel storage.
        pil_image.convert("1").save(path, format="PNG", optimize=True)
        return

    # PIL receives RGB data from PDFium, so saving through it avoids the
    # red/blue channel swap that OpenCV's BGR-oriented JPEG encoder would cause.
    pil_image.convert("RGB").save(
        path,
        format="JPEG",
        quality=jpeg_quality,
        optimize=True,
    )


def render_and_preprocess_page(document, index: int, pages_dir: Path, config: Config) -> tuple[int, Path, float]:
    """Render and preprocess one PDF page using a process-local document."""
    import numpy as np

    page = document[index]
    bitmap = None
    try:
        bitmap = page.render(scale=config.dpi / 72.0)
        rgb = np.asarray(bitmap.to_pil().convert("RGB"))
        processed, angle = preprocess_page(rgb, config)
        image_path = pages_dir / f"page-{index + 1:06d}{page_image_suffix(config)}"
        write_page_image(
            image_path,
            processed,
            config.binarization != "none",
            config.jpeg_quality,
        )
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


def build_preprocessed_pdf(
    source: Path,
    destination: Path | None,
    pages_dir: Path,
    config: Config,
) -> int:
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
    if destination is not None:
        import img2pdf

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


def run_ocr(source: Path, destination: Path, config: Config, sidecar: Path | None = None) -> None:
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
    if sidecar is not None:
        command.extend(("--sidecar", str(sidecar)))
    command.extend((str(source), str(destination)))
    LOG.info("    运行 OCR（语言：%s）", config.language)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0 or not destination.is_file():
        raise ProcessingError(f"OCRmyPDF 执行失败（退出码 {completed.returncode}）")


def is_cjk_character(character: str) -> bool:
    if not character:
        return False
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def is_markdown_list_item(line: str) -> bool:
    if line.startswith(("- ", "* ", "+ ")):
        return True
    first, separator, _rest = line.partition(" ")
    return bool(separator and len(first) > 1 and first[:-1].isdigit() and first[-1] in ".)")


def join_wrapped_lines(lines: Sequence[str]) -> str:
    if not lines:
        return ""
    if all(is_markdown_list_item(line) for line in lines):
        return "\n".join(lines)

    paragraph = lines[0]
    for line in lines[1:]:
        if paragraph.endswith("-") and line[:1].isascii() and line[:1].islower():
            paragraph = paragraph[:-1] + line
        elif is_cjk_character(paragraph[-1:]) or is_cjk_character(line[:1]):
            paragraph += line
        else:
            paragraph += " " + line
    return paragraph


def normalize_page_text(page_text: str) -> str:
    blocks: list[str] = []
    current: list[str] = []
    normalized = page_text.replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if line:
            current.append(line)
        elif current:
            blocks.append(join_wrapped_lines(current))
            current = []
    if current:
        blocks.append(join_wrapped_lines(current))
    return "\n\n".join(block for block in blocks if block)


def markdown_from_pages(title: str, pages: Sequence[str]) -> str:
    safe_title = " ".join(title.splitlines()).replace("#", "\\#")
    sections = [f"# {safe_title}"]
    for page_number, page_text in enumerate(pages, start=1):
        section = f"<!-- page: {page_number} -->"
        normalized = normalize_page_text(page_text)
        if normalized:
            section += f"\n\n{normalized}"
        sections.append(section)
    return "\n\n".join(sections).rstrip() + "\n"


def split_markdown_pages(content: str) -> tuple[str, list[tuple[int, str]]]:
    """Split generated Markdown without losing its page boundaries."""
    cleaned = WEREAD_SECTION_RE.sub("", content)
    markers = list(PAGE_MARKER_RE.finditer(cleaned))
    if not markers:
        return cleaned.strip(), []

    prefix = cleaned[: markers[0].start()].strip()
    pages: list[tuple[int, str]] = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(cleaned)
        pages.append((int(marker.group(1)), cleaned[marker.end() : end].strip()))
    return prefix, pages


def split_markdown_paragraphs(text: str) -> list[str]:
    return [paragraph.strip() for paragraph in re.split(r"\n[ \t]*\n", text.strip()) if paragraph.strip()]


def edge_candidate(paragraph: str) -> str | None:
    """Return a short single-line page-edge candidate suitable for repetition checks."""
    compact = " ".join(paragraph.split())
    if "\n" in paragraph or not compact or len(compact) > 120 or len(compact.split()) > 14:
        return None
    return compact


def normalize_running_header(text: str) -> str:
    compact = " ".join(text.split()).strip(" |—–-")
    compact = re.sub(
        r"\s+(?:[A-Za-z]{0,2}\d{1,4}|\d{1,4}[A-Za-z]{0,2})$",
        "",
        compact,
    )
    return compact.casefold()


def is_standalone_page_number(text: str) -> bool:
    compact = " ".join(text.split()).strip()
    return bool(
        re.fullmatch(r"(?:\d{1,4}|[ivxlcdm]{1,10}|[A-Za-z]?\W{0,2}\d{1,4})", compact, re.IGNORECASE)
    )


def recurring_edge_keys(pages: Sequence[tuple[int, str]], first: bool) -> set[str]:
    counts: dict[str, int] = {}
    for _page_number, page_text in pages:
        paragraphs = split_markdown_paragraphs(page_text)
        if not paragraphs:
            continue
        candidate = edge_candidate(paragraphs[0] if first else paragraphs[-1])
        if candidate is None or is_standalone_page_number(candidate):
            continue
        key = normalize_running_header(candidate)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count >= 3}


def format_trailing_footnotes(paragraphs: list[str]) -> list[str]:
    first_footnote = len(paragraphs)
    while first_footnote:
        paragraph = paragraphs[first_footnote - 1]
        if "\n" in paragraph or FOOTNOTE_RE.fullmatch(paragraph) is None:
            break
        first_footnote -= 1

    for index in range(first_footnote, len(paragraphs)):
        match = FOOTNOTE_RE.fullmatch(paragraphs[index])
        if match is not None:
            paragraphs[index] = f"> **【注 {match.group(1)}】** {match.group(2)}"
    return paragraphs


def clean_weread_page(page_text: str, header_keys: set[str], footer_keys: set[str]) -> str:
    paragraphs = split_markdown_paragraphs(page_text)
    if not paragraphs:
        return ""

    first = edge_candidate(paragraphs[0])
    if first is not None and (
        is_standalone_page_number(first) or normalize_running_header(first) in header_keys
    ):
        paragraphs.pop(0)

    if paragraphs:
        last = edge_candidate(paragraphs[-1])
        if last is not None and (
            is_standalone_page_number(last) or normalize_running_header(last) in footer_keys
        ):
            paragraphs.pop()

    return "\n\n".join(format_trailing_footnotes(paragraphs))


def postprocess_markdown_for_weread(
    content: str,
    max_section_bytes: int = WEREAD_SECTION_MAX_BYTES,
) -> str:
    """Create a conservative WeRead-oriented variant of generated Markdown."""
    if max_section_bytes < 1024:
        raise ValueError("max_section_bytes must be at least 1024")

    prefix, pages = split_markdown_pages(content)
    if not pages:
        return content.rstrip() + "\n"

    header_keys = recurring_edge_keys(pages, first=True)
    footer_keys = recurring_edge_keys(pages, first=False)
    cleaned_pages = [
        (page_number, clean_weread_page(page_text, header_keys, footer_keys))
        for page_number, page_text in pages
    ]

    sections = [prefix]
    section_number = 1
    section_heading = f"## 阅读分段 {section_number}"
    sections.append(section_heading)
    current_bytes = len((section_heading + "\n\n").encode("utf-8"))

    for page_number, page_text in cleaned_pages:
        page_block = f"<!-- page: {page_number} -->"
        if page_text:
            page_block += f"\n\n{page_text}"
        block_bytes = len((page_block + "\n\n").encode("utf-8"))
        if current_bytes > len((section_heading + "\n\n").encode("utf-8")) and (
            current_bytes + block_bytes > max_section_bytes
        ):
            section_number += 1
            section_heading = f"## 阅读分段 {section_number}"
            sections.append(section_heading)
            current_bytes = len((section_heading + "\n\n").encode("utf-8"))
        sections.append(page_block)
        current_bytes += block_bytes

    return "\n\n".join(part for part in sections if part).rstrip() + "\n"


def markdown_from_sidecar(title: str, sidecar: Path, page_count: int) -> str:
    text = sidecar.read_text(encoding="utf-8", errors="replace")
    pages = text.replace("\r\n", "\n").replace("\r", "\n").split("\f")
    if len(pages) < page_count:
        pages.extend([""] * (page_count - len(pages)))
    elif len(pages) > page_count:
        pages = pages[: page_count - 1] + ["\n".join(pages[page_count - 1 :])]
    return markdown_from_pages(title, pages)


def estimate_text_width(text: str, font_size: float) -> float:
    units = sum(1.0 if is_cjk_character(character) else 0.55 for character in text)
    return max(font_size * 0.5, units * font_size)


def group_positioned_lines(words: Sequence[PositionedWord]) -> list[PositionedLine]:
    lines: list[PositionedLine] = []
    for word in sorted(words, key=lambda item: (-item.y, item.x)):
        best_line = None
        best_distance = math.inf
        for line in lines:
            distance = abs(line.y - word.y)
            line_font_size = statistics.median(item.font_size for item in line.words)
            if distance <= max(1.0, min(line_font_size, word.font_size) * 0.4) and distance < best_distance:
                best_line = line
                best_distance = distance
        if best_line is None:
            lines.append(PositionedLine(word.y, [word]))
        else:
            best_line.words.append(word)
            best_line.y = sum(item.y for item in best_line.words) / len(best_line.words)
    for line in lines:
        line.words.sort(key=lambda item: item.x)
    return sorted(lines, key=lambda item: -item.y)


def find_column_gutter(lines: Sequence[PositionedLine], page_width: float) -> tuple[float, float] | None:
    words = [word for line in lines for word in line.words]
    width = max(1, int(math.ceil(page_width)))
    if len(words) < 8 or width < 100:
        return None

    difference = [0] * (width + 2)
    for word in words:
        start = max(0, min(width, int(math.floor(word.x))))
        end = max(start + 1, min(width + 1, int(math.ceil(word.x + word.width))))
        difference[start] += 1
        difference[end] -= 1
    coverage: list[int] = []
    active = 0
    for delta in difference[: width + 1]:
        active += delta
        coverage.append(active)

    search_start = int(width * 0.2)
    search_end = int(width * 0.8)
    maximum_coverage = max(coverage[search_start:search_end], default=0)
    threshold = max(1, min(maximum_coverage // 5, len(lines) // 10))
    minimum_width = max(8, int(width * 0.025))
    candidates: list[tuple[int, int, int]] = []
    run_start = None
    for position in range(search_start, search_end + 1):
        if coverage[position] <= threshold:
            if run_start is None:
                run_start = position
        elif run_start is not None:
            if position - run_start >= minimum_width:
                candidates.append((run_start, position, position - run_start))
            run_start = None
    if run_start is not None and search_end + 1 - run_start >= minimum_width:
        candidates.append((run_start, search_end + 1, search_end + 1 - run_start))

    ranked: list[tuple[int, int, float, int, int]] = []
    minimum_side_words = max(3, int(len(words) * 0.08))
    for start, end, span in candidates:
        midpoint = (start + end) / 2
        left_words = sum(word.x + word.width / 2 < midpoint for word in words)
        right_words = sum(word.x + word.width / 2 > midpoint for word in words)
        if left_words < minimum_side_words or right_words < minimum_side_words:
            continue
        paired_lines = sum(
            any(word.x + word.width / 2 < midpoint for word in line.words)
            and any(word.x + word.width / 2 > midpoint for word in line.words)
            for line in lines
        )
        ranked.append((paired_lines, span, -abs(midpoint - page_width / 2), start, end))
    if not ranked:
        return None
    _paired, _span, _distance, start, end = max(ranked)
    return float(start), float(end)


def positioned_line_text(words: Sequence[PositionedWord]) -> str:
    text = ""
    for word in sorted(words, key=lambda item: item.x):
        if not text:
            text = word.text
        elif is_cjk_character(text[-1:]) or is_cjk_character(word.text[:1]):
            text += word.text
        else:
            text += " " + word.text
    return text


def format_positioned_lines(lines: Sequence[PositionedLine]) -> str:
    populated = [line for line in lines if line.words]
    if not populated:
        return ""
    populated.sort(key=lambda item: -item.y)
    gaps = [populated[index].y - populated[index + 1].y for index in range(len(populated) - 1)]
    positive_gaps = [gap for gap in gaps if gap > 0.5]
    normal_gap = statistics.median(positive_gaps) if positive_gaps else 0.0
    baseline_x = min(word.x for line in populated for word in line.words)

    paragraphs: list[str] = []
    current: list[str] = []
    previous_line = None
    for line in populated:
        line_text = positioned_line_text(line.words)
        line_x = min(word.x for word in line.words)
        line_font_size = statistics.median(word.font_size for word in line.words)
        new_paragraph = False
        if previous_line is not None:
            vertical_gap = previous_line.y - line.y
            if normal_gap and vertical_gap > max(normal_gap * 1.45, line_font_size * 1.7):
                new_paragraph = True
            elif line_x > baseline_x + line_font_size * 1.4:
                new_paragraph = True
            elif is_markdown_list_item(line_text):
                new_paragraph = bool(current and not all(is_markdown_list_item(item) for item in current))
        if new_paragraph and current:
            paragraphs.append(join_wrapped_lines(current))
            current = []
        current.append(line_text)
        previous_line = line
    if current:
        paragraphs.append(join_wrapped_lines(current))
    return "\n\n".join(paragraphs)


def positioned_words_to_text(words: Sequence[PositionedWord], page_width: float) -> str:
    lines = group_positioned_lines(words)
    gutter = find_column_gutter(lines, page_width)
    if gutter is None:
        return format_positioned_lines(lines)

    gutter_start, gutter_end = gutter
    midpoint = (gutter_start + gutter_end) / 2
    sections: list[str] = []
    column_lines: list[PositionedLine] = []

    def flush_columns() -> None:
        if not column_lines:
            return
        left = [
            PositionedLine(line.y, [word for word in line.words if word.x + word.width / 2 < midpoint])
            for line in column_lines
        ]
        right = [
            PositionedLine(line.y, [word for word in line.words if word.x + word.width / 2 >= midpoint])
            for line in column_lines
        ]
        for column in (left, right):
            formatted = format_positioned_lines(column)
            if formatted:
                sections.append(formatted)
        column_lines.clear()

    for line in lines:
        crosses_gutter = any(word.x < midpoint < word.x + word.width for word in line.words)
        if crosses_gutter:
            flush_columns()
            sections.append(format_positioned_lines([line]))
        else:
            column_lines.append(line)
    flush_columns()
    return "\n\n".join(section for section in sections if section)


def positioned_text_from_pdf_page(page) -> str:
    words: list[PositionedWord] = []

    def collect(text, current_matrix, text_matrix, _font, font_size) -> None:
        cleaned = " ".join(text.split())
        if not cleaned:
            return
        x = text_matrix[4] * current_matrix[0] + text_matrix[5] * current_matrix[2] + current_matrix[4]
        y = text_matrix[4] * current_matrix[1] + text_matrix[5] * current_matrix[3] + current_matrix[5]
        size = max(1.0, abs(float(font_size)))
        words.append(PositionedWord(cleaned, float(x), float(y), size, estimate_text_width(cleaned, size)))

    plain_text = page.extract_text(visitor_text=collect) or ""
    if not words:
        return plain_text
    return positioned_words_to_text(words, float(page.mediabox.width))


def positioned_words_from_tsv(tsv: str) -> tuple[list[PositionedWord], float]:
    words: list[PositionedWord] = []
    page_width = 0.0
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        try:
            level = int(row.get("level", "0"))
            width = float(row.get("width", "0"))
            if level == 1:
                page_width = max(page_width, width)
            if level != 5:
                continue
            text = " ".join((row.get("text") or "").split())
            if not text:
                continue
            left = float(row.get("left", "0"))
            top = float(row.get("top", "0"))
            height = max(1.0, float(row.get("height", "0")))
            words.append(PositionedWord(text, left, -top, height, max(1.0, width)))
        except (TypeError, ValueError):
            continue
    if page_width <= 0 and words:
        page_width = max(word.x + word.width for word in words)
    return words, page_width


def markdown_from_page_images(title: str, pages_dir: Path, page_count: int, config: Config) -> str:
    tesseract = find_tesseract()
    if not tesseract:
        raise ProcessingError("无法生成 Markdown：未找到 Tesseract")
    image_paths = sorted(pages_dir.glob("page-*"))
    if len(image_paths) != page_count:
        raise ProcessingError(f"Markdown 页面图像数量不一致（{len(image_paths)} != {page_count}）")

    pages: list[str] = []
    total_words = 0
    for page_number, image_path in enumerate(image_paths, start=1):
        command = [
            tesseract,
            str(image_path),
            "stdout",
            "-l",
            config.language,
            "--psm",
            "3",
            "tsv",
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout or "").strip().splitlines()
            detail = diagnostic[-1] if diagnostic else f"退出码 {completed.returncode}"
            raise ProcessingError(f"第 {page_number} 页结构化 OCR 失败：{detail}")
        words, page_width = positioned_words_from_tsv(completed.stdout)
        total_words += len(words)
        pages.append(positioned_words_to_text(words, page_width) if words else "")
    if total_words == 0:
        raise ProcessingError("结构化 OCR 未识别到文字")
    return markdown_from_pages(title, pages)


def markdown_from_pdf(title: str, source: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(source), strict=False)
    pages = [positioned_text_from_pdf_page(page) for page in reader.pages]
    return markdown_from_pages(title, pages)


def write_markdown(destination: Path, content: str) -> None:
    staging = destination.with_name(f".{destination.name}.partial")
    if staging.exists():
        staging.unlink()
    try:
        staging.write_text(content, encoding="utf-8", newline="\n")
        os.replace(staging, destination)
    finally:
        if staging.exists():
            staging.unlink()


def write_markdown_outputs(
    markdown_output: Path,
    content: str,
    weread: bool,
) -> tuple[Path, Path | None]:
    write_markdown(markdown_output, content)
    weread_output = None
    if weread:
        weread_output = weread_markdown_path_for(markdown_output)
        write_markdown(weread_output, postprocess_markdown_for_weread(content))
    return markdown_output, weread_output


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
    if IS_WINDOWS:
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
    markdown_output = markdown_path_for(output)
    weread_output = weread_markdown_path_for(markdown_output)
    if config.markdown_only and markdown_output.exists() and not config.overwrite:
        if config.weread_markdown and not weread_output.exists():
            try:
                content = markdown_output.read_text(encoding="utf-8")
                write_markdown(weread_output, postprocess_markdown_for_weread(content))
                return Result(
                    source=source,
                    output=None,
                    status="ok",
                    elapsed_seconds=time.monotonic() - started,
                    message="原始 Markdown 已存在；已补生成微信读书版 Markdown",
                    markdown_output=markdown_output,
                    weread_markdown_output=weread_output,
                )
            except Exception as exc:
                return Result(
                    source=source,
                    output=None,
                    status="failed",
                    elapsed_seconds=time.monotonic() - started,
                    message=f"无法补生成微信读书版 Markdown：{exc}",
                    markdown_output=markdown_output,
                )
        return Result(
            source,
            None,
            "skipped",
            0.0,
            message="Markdown 输出已存在",
            markdown_output=markdown_output,
            weread_markdown_output=weread_output if weread_output.exists() else None,
        )
    if not config.markdown_only and output.exists() and not config.overwrite:
        if config.markdown and not markdown_output.exists():
            try:
                content = markdown_from_pdf(source.stem, output)
                _, written_weread = write_markdown_outputs(
                    markdown_output,
                    content,
                    config.weread_markdown,
                )
                return Result(
                    source,
                    output,
                    "ok",
                    time.monotonic() - started,
                    message="PDF 已存在；已从文字层补生成 Markdown",
                    markdown_output=markdown_output,
                    weread_markdown_output=written_weread,
                )
            except Exception as exc:
                return Result(
                    source,
                    output,
                    "skipped",
                    time.monotonic() - started,
                    message=f"输出已存在，但无法补生成 Markdown：{exc}",
                )
        if config.weread_markdown and markdown_output.exists() and not weread_output.exists():
            try:
                content = markdown_output.read_text(encoding="utf-8")
                write_markdown(weread_output, postprocess_markdown_for_weread(content))
                return Result(
                    source=source,
                    output=output,
                    status="ok",
                    elapsed_seconds=time.monotonic() - started,
                    message="PDF 和原始 Markdown 已存在；已补生成微信读书版 Markdown",
                    markdown_output=markdown_output,
                    weread_markdown_output=weread_output,
                )
            except Exception as exc:
                return Result(
                    source=source,
                    output=output,
                    status="failed",
                    elapsed_seconds=time.monotonic() - started,
                    message=f"无法补生成微信读书版 Markdown：{exc}",
                    markdown_output=markdown_output,
                )
        return Result(
            source,
            output,
            "skipped",
            0.0,
            message="输出已存在",
            markdown_output=markdown_output if markdown_output.exists() else None,
            weread_markdown_output=weread_output if weread_output.exists() else None,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_dir, temp_manager = safe_temp_directory(output, config.keep_temp)
    pages_dir = temp_dir / "pages"
    pages_dir.mkdir()
    preprocessed = temp_dir / "preprocessed.pdf"
    ocr_result = temp_dir / "ocr.pdf"
    sidecar = temp_dir / "ocr.txt" if config.markdown else None
    compressed_result = temp_dir / "compressed.pdf"
    compressed = False
    warnings: list[str] = []
    before_bytes = 0
    after_bytes = 0
    markdown_content: str | None = None
    try:
        LOG.info("处理：%s", source)
        preprocessed_destination = None if config.markdown_only else preprocessed
        page_count = build_preprocessed_pdf(source, preprocessed_destination, pages_dir, config)
        if config.markdown_only:
            LOG.info("    已生成 %d 页预处理图像", page_count)
            markdown_content = markdown_from_page_images(source.stem, pages_dir, page_count, config)
            _, written_weread = write_markdown_outputs(
                markdown_output,
                markdown_content,
                config.weread_markdown,
            )
            LOG.info("    已生成 Markdown：%s", markdown_output)
            if written_weread is not None:
                LOG.info("    已生成微信读书版 Markdown：%s", written_weread)
            return Result(
                source=source,
                output=None,
                status="ok",
                elapsed_seconds=time.monotonic() - started,
                markdown_output=markdown_output,
                weread_markdown_output=written_weread,
            )

        LOG.info("    已生成 %d 页预处理 PDF", page_count)
        run_ocr(preprocessed, ocr_result, config, sidecar=sidecar)
        ocr_pages, ocr_text = pdf_text_signature(ocr_result)
        if ocr_pages != page_count:
            raise ProcessingError(f"OCR 后页数变化（{page_count} -> {ocr_pages}）")
        recognized_chars = sum(map(len, ocr_text))
        if recognized_chars == 0:
            notice = "OCR 完成，但未提取到任何文字；请检查语言包和页面质量"
            warnings.append(notice)
            LOG.warning("    %s", notice)
        else:
            LOG.info("    OCR 校验通过：%d 页，%d 个非空白字符", ocr_pages, recognized_chars)
        if sidecar is not None:
            try:
                markdown_content = markdown_from_page_images(source.stem, pages_dir, page_count, config)
            except Exception as exc:
                try:
                    if not sidecar.is_file():
                        raise ProcessingError("OCRmyPDF 未生成文字 sidecar")
                    markdown_content = markdown_from_sidecar(source.stem, sidecar, page_count)
                    notice = f"结构化 Markdown 解析失败，已回退到纯文本 sidecar：{exc}"
                    warnings.append(notice)
                    LOG.warning("    %s", notice)
                except Exception as fallback_exc:
                    notice = f"Markdown 生成失败：{fallback_exc}"
                    warnings.append(notice)
                    LOG.warning("    %s", notice)
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
                    notice = f"压缩校验未通过：{detail}；已保留原 OCR 文件"
                    warnings.append(notice)
                    LOG.warning("    %s", notice)
            except ProcessingError as exc:
                # OCR succeeded, so a missing/failed optional compressor should not
                # discard the useful searchable PDF.
                notice = f"压缩未完成：{exc}；已保留原 OCR 文件"
                warnings.append(notice)
                LOG.warning("    %s", notice)

        after_bytes = selected.stat().st_size
        if compressed and after_bytes > threshold_bytes:
            notice = (
                f"压缩后仍为 {format_mb(after_bytes)}，高于阈值；"
                "可降低 --compression-dpi 或 --jpeg-quality"
            )
            warnings.append(notice)
            LOG.warning("    %s", notice)
        staging = output.with_name(f".{output.name}.partial")
        if staging.exists():
            staging.unlink()
        shutil.copyfile(selected, staging)
        os.replace(staging, output)
        written_markdown = None
        written_weread = None
        if markdown_content is not None:
            try:
                written_markdown, written_weread = write_markdown_outputs(
                    markdown_output,
                    markdown_content,
                    config.weread_markdown,
                )
                LOG.info("    已生成 Markdown：%s", markdown_output)
                if written_weread is not None:
                    LOG.info("    已生成微信读书版 Markdown：%s", written_weread)
            except Exception as exc:
                notice = f"Markdown 写入失败：{exc}"
                warnings.append(notice)
                LOG.warning("    %s", notice)
        return Result(
            source=source,
            output=output,
            status="ok",
            elapsed_seconds=time.monotonic() - started,
            compressed=compressed,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            message="；".join(warnings),
            markdown_output=written_markdown,
            weread_markdown_output=written_weread,
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
            outputs = []
            if result.output is not None:
                outputs.append(str(result.output))
            if result.markdown_output is not None:
                outputs.append(str(result.markdown_output))
            if result.weread_markdown_output is not None:
                outputs.append(str(result.weread_markdown_output))
            print(f"  [{label}] {result.source.name} -> {', '.join(outputs)}{compression}{warning}")
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
