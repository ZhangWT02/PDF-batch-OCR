from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import batch_ocr


def make_config(root: Path, **overrides) -> batch_ocr.Config:
    values = dict(
        input_dir=root,
        output_dir=root / "ocr_output",
        recursive=False,
        overwrite=False,
        dpi=300,
        language="chi_sim+eng",
        jobs=1,
        preprocess_jobs=1,
        auto_rotate=True,
        deskew_max_angle=12.0,
        deskew_min_angle=0.1,
        denoise="median",
        denoise_strength=7.0,
        binarization="none",
        adaptive_block_size=41,
        adaptive_c=15,
        compression_threshold_mb=50.0,
        compression_dpi=200,
        jpeg_quality=72,
        output_suffix="_ocr",
        markdown=True,
        markdown_only=False,
        weread_markdown=False,
        keep_temp=False,
    )
    values.update(overrides)
    return batch_ocr.Config(**values)


class BatchOcrTests(unittest.TestCase):
    def test_defaults_preserve_color_with_jpeg_encoding(self) -> None:
        args = batch_ocr.build_parser().parse_args(["input"])
        self.assertEqual(args.language, "chi_sim+eng")
        self.assertEqual(args.compression_threshold_mb, 50.0)
        self.assertEqual(args.binarization, "none")
        self.assertEqual(args.jpeg_quality, 72)
        self.assertTrue(args.markdown)
        self.assertFalse(args.markdown_only)
        self.assertFalse(args.weread_markdown)
        self.assertEqual(args.denoise, "median")
        self.assertEqual(args.preprocess_jobs, min(4, batch_ocr.os.cpu_count() or 1))

    def test_page_image_suffix_uses_jpeg_for_color_and_png_for_bilevel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(batch_ocr.page_image_suffix(make_config(root)), ".jpg")
            self.assertEqual(
                batch_ocr.page_image_suffix(make_config(root, binarization="adaptive")),
                ".png",
            )

    def test_color_preprocessing_does_not_collapse_rgb_to_grayscale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = make_config(root, denoise="none")
            rgb = object()
            gray = object()
            fake_cv2 = SimpleNamespace(
                COLOR_RGB2GRAY=1,
                cvtColor=lambda image, code: gray,
            )

            with (
                patch.dict(batch_ocr.sys.modules, {"cv2": fake_cv2}),
                patch.object(batch_ocr, "estimate_skew_angle", return_value=0.0),
            ):
                processed, angle = batch_ocr.preprocess_page(rgb, config)

            self.assertIs(processed, rgb)
            self.assertEqual(angle, 0.0)

    def test_color_page_encoder_uses_configured_jpeg_quality(self) -> None:
        saved = {}

        class FakeImage:
            def convert(self, mode):
                saved["mode"] = mode
                return self

            def save(self, path, **kwargs):
                saved["path"] = path
                saved.update(kwargs)

        fake_image = FakeImage()
        fake_pil = SimpleNamespace(Image=SimpleNamespace(fromarray=lambda image: fake_image))

        with patch.dict(batch_ocr.sys.modules, {"PIL": fake_pil}):
            batch_ocr.write_page_image(Path("page.jpg"), object(), False, 83)

        self.assertEqual(saved["mode"], "RGB")
        self.assertEqual(saved["format"], "JPEG")
        self.assertEqual(saved["quality"], 83)
        self.assertTrue(saved["optimize"])

    def test_preprocess_jobs_can_be_configured(self) -> None:
        args = batch_ocr.build_parser().parse_args(["input", "--preprocess-jobs", "2"])
        self.assertEqual(args.preprocess_jobs, 2)

    def test_discovery_excludes_outputs_and_existing_ocr_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "book.pdf").touch()
            (root / "book_ocr.pdf").touch()
            (root / "nested").mkdir()
            (root / "nested" / "second.PDF").touch()
            (root / "ocr_output").mkdir()
            (root / "ocr_output" / "old.pdf").touch()
            config = make_config(root, recursive=True)

            found = batch_ocr.discover_pdfs(config)

            self.assertEqual(found, [root / "book.pdf", root / "nested" / "second.PDF"])

    def test_output_path_preserves_recursive_folder_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = make_config(root, recursive=True)
            source = root / "history" / "book.pdf"
            expected = root / "ocr_output" / "history" / "book_ocr.pdf"
            self.assertEqual(batch_ocr.output_path_for(source, config), expected)
            self.assertEqual(batch_ocr.markdown_path_for(expected), expected.with_suffix(".md"))
            self.assertEqual(
                batch_ocr.weread_markdown_path_for(expected.with_suffix(".md")),
                expected.with_name("book_ocr_weread.md"),
            )

    def test_weread_postprocessing_removes_running_headers_and_formats_footnotes(self) -> None:
        markdown = batch_ocr.markdown_from_pages(
            "Sample Book",
            [
                "Sample Book 10\n\nFirst body paragraph.\n\n1. First citation.",
                "Sample Book 11\n\nSecond body paragraph.\n\n2, Second citation.",
                "Sample Book 12\n\n1. First list item\n2. Second list item",
            ],
        )

        result = batch_ocr.postprocess_markdown_for_weread(markdown)

        self.assertNotIn("Sample Book 10", result)
        self.assertNotIn("Sample Book 11", result)
        self.assertNotIn("Sample Book 12", result)
        self.assertIn("First body paragraph.", result)
        self.assertIn("> **【注 1】** First citation.", result)
        self.assertIn("> **【注 2】** Second citation.", result)
        self.assertIn("1. First list item\n2. Second list item", result)
        self.assertEqual(result.count("<!-- page:"), 3)
        self.assertIn("## 阅读分段 1", result)

    def test_weread_postprocessing_is_idempotent(self) -> None:
        markdown = batch_ocr.markdown_from_pages(
            "Sample Book",
            [f"Sample Book {page}\n\nBody {page}." for page in range(1, 5)],
        )

        once = batch_ocr.postprocess_markdown_for_weread(markdown)
        twice = batch_ocr.postprocess_markdown_for_weread(once)

        self.assertEqual(twice, once)

    def test_weread_postprocessing_splits_large_books_below_configured_limit(self) -> None:
        markdown = batch_ocr.markdown_from_pages(
            "Large Book",
            [f"Large Book {page}\n\n" + (f"page-{page} " * 700) for page in range(1, 7)],
        )

        result = batch_ocr.postprocess_markdown_for_weread(markdown, max_section_bytes=10_000)
        sections = result.split("## 阅读分段 ")[1:]

        self.assertGreater(len(sections), 1)
        self.assertTrue(all(len(section.encode("utf-8")) <= 10_000 for section in sections))

    def test_sidecar_to_markdown_preserves_pages_paragraphs_and_lists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sidecar = Path(directory) / "ocr.txt"
            sidecar.write_text(
                "第一段第一行\n继续内容\n\n左栏结束。\fInter-\nnational text\n\n- one\n- two\n",
                encoding="utf-8",
            )

            markdown = batch_ocr.markdown_from_sidecar("双栏#样本", sidecar, 2)

            self.assertIn("# 双栏\\#样本", markdown)
            self.assertIn("<!-- page: 1 -->", markdown)
            self.assertIn("第一段第一行继续内容\n\n左栏结束。", markdown)
            self.assertIn("<!-- page: 2 -->", markdown)
            self.assertIn("International text", markdown)
            self.assertIn("- one\n- two", markdown)

    def test_positioned_words_reorder_two_columns_before_joining_paragraphs(self) -> None:
        word = batch_ocr.PositionedWord
        words = [
            word("LEFT COLUMN", 20, 100, 10, 72),
            word("RIGHT COLUMN", 230, 100, 10, 84),
            word("First left line.", 20, 90, 10, 88),
            word("First right line.", 230, 90, 10, 92),
            word("Second left line.", 20, 80, 10, 96),
            word("Second right line.", 230, 80, 10, 100),
            word("Final left paragraph.", 20, 60, 10, 112),
            word("Final right paragraph.", 230, 60, 10, 116),
        ]

        text = batch_ocr.positioned_words_to_text(words, 400)

        self.assertLess(text.index("LEFT COLUMN"), text.index("First left line."))
        self.assertLess(text.index("First left line."), text.index("Final left paragraph."))
        self.assertLess(text.index("Final left paragraph."), text.index("RIGHT COLUMN"))
        self.assertLess(text.index("RIGHT COLUMN"), text.index("First right line."))
        self.assertIn("Second left line.\n\nFinal left paragraph.", text)

    def test_run_ocr_requests_sidecar_before_positional_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.pdf"
            destination = root / "output.pdf"
            sidecar = root / "output.txt"
            source.touch()
            destination.touch()
            config = make_config(root)

            with patch.object(
                batch_ocr.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run:
                batch_ocr.run_ocr(source, destination, config, sidecar=sidecar)

            command = run.call_args.args[0]
            sidecar_index = command.index("--sidecar")
            self.assertEqual(command[sidecar_index + 1], str(sidecar))
            self.assertLess(sidecar_index, command.index(str(source)))

    def test_weighted_median_prefers_long_text_lines(self) -> None:
        self.assertEqual(batch_ocr.weighted_median([-8.0, 1.5, 2.0], [1.0, 20.0, 10.0]), 1.5)

    def test_angle_is_measured_from_horizontal_or_vertical_text_axis(self) -> None:
        self.assertEqual(batch_ocr.angle_from_nearest_text_axis(2.0), 2.0)
        self.assertEqual(batch_ocr.angle_from_nearest_text_axis(88.0), -2.0)
        self.assertEqual(batch_ocr.angle_from_nearest_text_axis(-88.0), 2.0)

    def test_compression_only_runs_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "book.pdf"
            source.write_bytes(b"source")
            output = root / "out" / "book_ocr.pdf"
            config = make_config(root, compression_threshold_mb=0.00005)

            def fake_build(_source, destination, _pages_dir, _config):
                destination.write_bytes(b"preprocessed")
                return 1

            def fake_ocr(_source, destination, _config, sidecar=None):
                destination.write_bytes(b"x" * 100)
                sidecar.write_text("第一行\n第二行", encoding="utf-8")

            def fake_compress(_source, destination, _config):
                destination.write_bytes(b"x" * 60)

            with (
                patch.object(batch_ocr, "build_preprocessed_pdf", side_effect=fake_build),
                patch.object(batch_ocr, "run_ocr", side_effect=fake_ocr),
                patch.object(batch_ocr, "markdown_from_page_images", return_value="# book\n\n第一行第二行\n"),
                patch.object(batch_ocr, "compress_images_only", side_effect=fake_compress) as compressor,
                patch.object(batch_ocr, "pdf_text_signature", return_value=(1, ("文字",))),
                patch.object(batch_ocr, "verify_compressed_pdf", return_value=(True, "ok")),
            ):
                result = batch_ocr.process_one(source, output, config)

            self.assertEqual(result.status, "ok")
            self.assertTrue(result.compressed)
            self.assertEqual(output.stat().st_size, 60)
            self.assertEqual(result.markdown_output, output.with_suffix(".md"))
            self.assertIn("第一行第二行", result.markdown_output.read_text(encoding="utf-8"))
            compressor.assert_called_once()

    def test_existing_pdf_backfills_missing_markdown_without_rerunning_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "book.pdf"
            source.touch()
            output = root / "ocr_output" / "book_ocr.pdf"
            output.parent.mkdir()
            output.write_bytes(b"existing PDF")
            config = make_config(root)

            with (
                patch.object(batch_ocr, "markdown_from_pdf", return_value="# book\n\n正文\n") as extractor,
                patch.object(batch_ocr, "run_ocr") as ocr,
            ):
                result = batch_ocr.process_one(source, output, config)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.markdown_output, output.with_suffix(".md"))
            self.assertEqual(result.markdown_output.read_text(encoding="utf-8"), "# book\n\n正文\n")
            extractor.assert_called_once_with("book", output)
            ocr.assert_not_called()

    def test_markdown_only_skips_pdf_ocr_and_writes_only_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "book.pdf"
            source.write_bytes(b"source")
            output = root / "ocr_output" / "book_ocr.pdf"
            config = make_config(root, markdown_only=True)

            def fake_build(_source, destination, _pages_dir, _config):
                self.assertIsNone(destination)
                return 1

            with (
                patch.object(batch_ocr, "build_preprocessed_pdf", side_effect=fake_build),
                patch.object(batch_ocr, "markdown_from_page_images", return_value="# book\n\n正文\n"),
                patch.object(batch_ocr, "run_ocr") as ocr,
                patch.object(batch_ocr, "compress_images_only") as compressor,
            ):
                result = batch_ocr.process_one(source, output, config)

            self.assertEqual(result.status, "ok")
            self.assertIsNone(result.output)
            self.assertFalse(output.exists())
            self.assertEqual(result.markdown_output, output.with_suffix(".md"))
            self.assertEqual(result.markdown_output.read_text(encoding="utf-8"), "# book\n\n正文\n")
            ocr.assert_not_called()
            compressor.assert_not_called()

    def test_markdown_only_ignores_an_existing_pdf_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "book.pdf"
            source.write_bytes(b"source")
            output = root / "ocr_output" / "book_ocr.pdf"
            output.parent.mkdir()
            output.write_bytes(b"existing PDF")
            config = make_config(root, markdown_only=True)

            with (
                patch.object(batch_ocr, "build_preprocessed_pdf", return_value=1) as build,
                patch.object(batch_ocr, "markdown_from_page_images", return_value="# book\n\n新识别正文\n"),
                patch.object(batch_ocr, "markdown_from_pdf") as existing_pdf_extractor,
            ):
                result = batch_ocr.process_one(source, output, config)

            self.assertEqual(result.status, "ok")
            self.assertEqual(output.read_bytes(), b"existing PDF")
            self.assertIn("新识别正文", result.markdown_output.read_text(encoding="utf-8"))
            build.assert_called_once()
            existing_pdf_extractor.assert_not_called()

    def test_weread_flag_writes_an_additional_markdown_variant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "book.pdf"
            source.write_bytes(b"source")
            output = root / "ocr_output" / "book_ocr.pdf"
            config = make_config(root, markdown_only=True, weread_markdown=True)
            markdown = batch_ocr.markdown_from_pages(
                "Book",
                [f"Book {page}\n\nBody {page}." for page in range(1, 4)],
            )

            with (
                patch.object(batch_ocr, "build_preprocessed_pdf", return_value=3),
                patch.object(batch_ocr, "markdown_from_page_images", return_value=markdown),
            ):
                result = batch_ocr.process_one(source, output, config)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.markdown_output, output.with_suffix(".md"))
            self.assertEqual(
                result.weread_markdown_output,
                output.with_name("book_ocr_weread.md"),
            )
            self.assertTrue(result.markdown_output.exists())
            self.assertTrue(result.weread_markdown_output.exists())
            weread_text = result.weread_markdown_output.read_text(encoding="utf-8")
            self.assertNotIn("Book 1", weread_text)
            self.assertIn("Body 1.", weread_text)

    def test_markdown_only_rejects_no_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser = batch_ocr.build_parser()
            args = parser.parse_args([directory, "--markdown-only", "--no-markdown"])
            with self.assertRaises(SystemExit):
                batch_ocr.validate_args(args, parser)

    def test_weread_markdown_rejects_no_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser = batch_ocr.build_parser()
            args = parser.parse_args([directory, "--weread-markdown", "--no-markdown"])
            with self.assertRaises(SystemExit):
                batch_ocr.validate_args(args, parser)

    def test_compression_uses_valid_ghostscript_filter_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input.pdf"
            destination = root / "output.pdf"
            source.touch()
            config = make_config(root)

            with (
                patch.object(batch_ocr, "find_ghostscript", return_value="gswin64c"),
                patch.object(
                    batch_ocr.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run,
            ):
                batch_ocr.compress_images_only(source, destination, config)

            command = run.call_args.args[0]
            self.assertIn("-sColorImageFilter=DCTEncode", command)
            self.assertIn("-sGrayImageFilter=DCTEncode", command)
            self.assertIn("-sMonoImageFilter=CCITTFaxEncode", command)
            self.assertNotIn("-sColorImageFilter=/DCTEncode", command)

    def test_find_ghostscript_checks_active_conda_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conda_prefix = Path(directory)
            executable = conda_prefix / "Library" / "bin" / "gswin64c.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()

            with (
                patch.object(batch_ocr.shutil, "which", return_value=None),
                patch.object(batch_ocr, "IS_WINDOWS", True),
                patch.dict(batch_ocr.os.environ, {"CONDA_PREFIX": str(conda_prefix)}),
            ):
                self.assertEqual(batch_ocr.find_ghostscript(), str(executable))

    def test_compression_error_includes_ghostscript_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = make_config(root)
            with (
                patch.object(batch_ocr, "find_ghostscript", return_value="gswin64c"),
                patch.object(
                    batch_ocr.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=255,
                        stdout="",
                        stderr="Unrecoverable error: rangecheck in .putdeviceprops\n",
                    ),
                ),
            ):
                with self.assertRaisesRegex(batch_ocr.ProcessingError, "rangecheck"):
                    batch_ocr.compress_images_only(root / "input.pdf", root / "output.pdf", config)


if __name__ == "__main__":
    unittest.main()
