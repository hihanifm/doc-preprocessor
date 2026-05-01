# Sample Documents

Each sample here is a reference document for a specific test plan format (Word and/or digital PDF).

## How to add support for a new format

1. Put a representative sample `.docx` and/or `.pdf` here (one file = one format).
2. Ask your coding agent to create an extractor:

   > "Look at `samples/your_new_doc.docx` (or `.pdf`). Read it with `readers.document_reader.read_document`
   > (run: `python3 -c "from readers.document_reader import read_document; print(read_document('samples/your_new_doc.docx'))"`)
   > For PDF-only samples use the same call with a `.pdf` path.
   > Then write a new extractor in `extractors/` that extends `BaseExtractor`.
   > Implement `matches()` to detect this format and `extract()` to parse it rule-based.
   > Use `extractors/user_management.py` as a reference."

3. Restart the app — it auto-discovers all extractors.

### Regenerating `sample_test_plan.pdf`

The PDF sample is committed as a binary. To regenerate (optional):

```bash
pip install reportlab
python3 scripts/generate_sample_pdf.py
```

## Available samples

| File | Format | Extractor |
|---|---|---|
| `sample_test_plan.docx` | User Management format | `extractors/user_management.py` |
| `sample_test_plan.pdf` | Same logical layout as the Word sample (digital PDF with table) | `extractors/user_management.py` |
