# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-10

### Added

- Batch discovery of scanned PDF files, including recursive mode.
- Per-page rasterization, small-angle deskewing, denoising and binarization.
- Controlled multi-process preprocessing with stable page ordering.
- Chinese and English OCR through OCRmyPDF and Tesseract.
- Conditional image compression above a configurable file-size threshold.
- Page-count and extracted-text verification after compression.
- Windows/Conda setup and troubleshooting documentation.
