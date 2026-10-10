# Contributing

Thanks for helping improve PDF Batch OCR.

## Development setup

```powershell
conda env create -f environment.yml
conda activate pdf-batch-ocr
```

On Windows, if Tesseract language data and configuration files are split across
two Conda directories, run:

```powershell
.\scripts\repair_conda_tessdata.ps1
```

## Before opening a pull request

```powershell
python -m py_compile .\batch_ocr.py
python -m unittest discover -s tests -v
```

Keep changes focused and include a regression test when fixing a bug. Never
commit scanned books, OCR outputs, credentials, or copyrighted test documents.
Use small, redistributable fixtures if an integration test needs a PDF.

## Pull requests

- Explain the user-facing problem and the chosen solution.
- Document new command-line flags in `README.md`.
- Preserve searchable text and page order in PDF pipeline changes.
- Note memory or performance implications for parallel processing changes.
