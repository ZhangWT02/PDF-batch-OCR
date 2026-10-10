## Summary

Describe the problem and the resulting user-visible behavior.

## Validation

- [ ] `python -m py_compile batch_ocr.py tests/test_batch_ocr.py`
- [ ] `python -m unittest discover -s tests -v`
- [ ] New or changed CLI options are documented
- [ ] No PDFs, OCR outputs, credentials or private paths are included

## PDF pipeline checklist

- [ ] Page order is preserved
- [ ] Searchable text is preserved where applicable
- [ ] Memory and performance implications are documented
