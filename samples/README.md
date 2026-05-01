# Sample Documents

Each `.docx` file in this folder is a reference document for a specific test plan format.

## How to add support for a new format

1. Put a representative sample `.docx` here (one file = one format).
2. Ask your coding agent to create an extractor:

   > "Look at `samples/your_new_doc.docx`. Read it with `readers/docx_reader.py`
   > (run: `python3 -c "from readers.docx_reader import read_docx; print(read_docx('samples/your_new_doc.docx'))"`)
   > Then write a new extractor in `extractors/` that extends `BaseExtractor`.
   > Implement `matches()` to detect this format and `extract()` to parse it rule-based.
   > Use `extractors/user_management.py` as a reference."

3. Restart the app — it auto-discovers all extractors.

## Available samples

| File | Format | Extractor |
|---|---|---|
| `sample_test_plan.docx` | User Management format | `extractors/user_management.py` |
