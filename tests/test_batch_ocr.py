from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
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
        binarization="adaptive",
        adaptive_block_size=41,
        adaptive_c=15,
        compression_threshold_mb=50.0,
        compression_dpi=200,
        jpeg_quality=72,
        output_suffix="_ocr",
        keep_temp=False,
    )
    values.update(overrides)
    return batch_ocr.Config(**values)


class BatchOcrTests(unittest.TestCase):
    def test_defaults_use_chinese_and_english_and_50_mb_threshold(self) -> None:
        args = batch_ocr.build_parser().parse_args(["input"])
        self.assertEqual(args.language, "chi_sim+eng")
        self.assertEqual(args.compression_threshold_mb, 50.0)
        self.assertEqual(args.binarization, "adaptive")
        self.assertEqual(args.denoise, "median")
        self.assertEqual(args.preprocess_jobs, min(4, batch_ocr.os.cpu_count() or 1))

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

            def fake_ocr(_source, destination, _config):
                destination.write_bytes(b"x" * 100)

            def fake_compress(_source, destination, _config):
                destination.write_bytes(b"x" * 60)

            with (
                patch.object(batch_ocr, "build_preprocessed_pdf", side_effect=fake_build),
                patch.object(batch_ocr, "run_ocr", side_effect=fake_ocr),
                patch.object(batch_ocr, "compress_images_only", side_effect=fake_compress) as compressor,
                patch.object(batch_ocr, "pdf_text_signature", return_value=(1, ("文字",))),
                patch.object(batch_ocr, "verify_compressed_pdf", return_value=(True, "ok")),
            ):
                result = batch_ocr.process_one(source, output, config)

            self.assertEqual(result.status, "ok")
            self.assertTrue(result.compressed)
            self.assertEqual(output.stat().st_size, 60)
            compressor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
