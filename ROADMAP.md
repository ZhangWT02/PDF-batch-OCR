# Roadmap

[简体中文](ROADMAP.zh-CN.md)

This roadmap describes the next usability and adoption milestones for PDF Batch
OCR. Dates are intentionally omitted until the packaging approach and maintenance
cost are understood. Each deliverable should be tracked by a separate GitHub issue.

## Near term: project demo ([#1](https://github.com/ZhangWT02/PDF-batch-OCR/issues/1))

Create a 15–30 second animated demo for the English and Chinese READMEs.

Completion criteria:

- Use a small redistributable or project-authored sample PDF.
- Describe the input precisely as a PDF without an embedded searchable text layer.
- Show one batch command, abbreviated processing progress, and a successful search
  in the generated PDF.
- Do not expose private paths, documents, or personal information.
- Keep the GIF legible at README width and reasonably small for repository visitors.

## v0.2.0: one-click Windows installation ([#2](https://github.com/ZhangWT02/PDF-batch-OCR/issues/2))

Provide a supported installation path for Windows users that does not require them
to assemble a Python environment manually.

Completion criteria:

- Install the `pdf-batch-ocr` command on a clean supported Windows system.
- Detect or provide clear handling for Tesseract, the requested language data,
  Ghostscript, and other runtime dependencies.
- Provide actionable diagnostics when a prerequisite is unavailable.
- Support uninstall and upgrade without deleting user documents or OCR outputs.
- Document third-party licenses and review redistribution constraints before
  bundling external programs.
- Publish the installer and checksums through a GitHub Release.

The packaging design should be decided in its issue before implementation. In
particular, bundling third-party executables and downloading prerequisites during
installation have different maintenance, security, and licensing implications.

## v0.3.0: desktop graphical interface ([#3](https://github.com/ZhangWT02/PDF-batch-OCR/issues/3))

Add a focused desktop interface for people who do not want to use a terminal. The
CLI remains supported and the GUI should call shared application logic rather than
maintain a separate OCR pipeline.

Minimum scope:

- Select input and output folders.
- Configure recursive discovery, OCR languages, preprocessing profile, and worker
  count.
- Display the file queue, current stage, progress, and useful error messages.
- Allow a running batch to be cancelled safely.
- Open the output folder when processing finishes.
- Preserve existing skip, overwrite, validation, and failure-isolation behavior.

Out of scope for the first GUI release:

- Editing individual PDF pages.
- Manually correcting recognized text.
- Cloud OCR or account features.

## Tracking the roadmap

Use one GitHub issue per deliverable and attach it to the corresponding milestone:

1. [Add an animated README demo](https://github.com/ZhangWT02/PDF-batch-OCR/issues/1) → `v0.1.x`
2. [Provide a one-click Windows installer](https://github.com/ZhangWT02/PDF-batch-OCR/issues/2) → `v0.2.0`
3. [Build a desktop GUI for batch OCR](https://github.com/ZhangWT02/PDF-batch-OCR/issues/3) → `v0.3.0`

A GitHub Project board can be added when there are enough related issues to need
cross-milestone planning. For these three deliverables, issues and milestones are
sufficient.
