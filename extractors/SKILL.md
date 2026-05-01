---
name: doc-preprocessor-extractors
description: How to add and reason about test-plan extractors (template matching + row shape) in this repo.
---

# Skill: Extractor templates & test-case row format

Use this when you need to **support a new `.docx` layout**, **debug “no template matched”**, or **generate extractor code** from a sample document.

## What an “extractor” is here

- **Not** an LLM prompt. Extractors are **Python classes** that implement deterministic parsing.
- Each extractor is a **template profile**: “if the document looks like *this*, parse it *this way*.”
- The web app reads `.docx` → **plain text** (`readers/docx_reader.py`) → picks **one** extractor whose `matches(doc_text)` is true → calls `extract(doc_text, filename)` → builds rows → Excel.

## End-to-end pipeline (for agents)

1. **Normalize input**  
   Everything downstream sees **`doc_text`**: headings become `## Heading`, tables become pipe-separated lines. Order matches the Word document.  
   Inspect any file with:
   ```bash
   python3 -c "from readers.docx_reader import read_docx; print(read_docx('samples/your_file.docx'))"
   ```

2. **Detect format (`matches`)**  
   Implement **`matches(self, doc_text: str) -> bool`**.  
   - Must be **cheap** and **specific** (avoid matching unrelated docs).  
   - Prefer **unique signatures**: distinctive heading patterns, fixed phrases, table headers, ID regexes, etc.  
   - **First matching extractor wins** (`extractors/__init__.py` iterates registered extractors in module discovery order). If two could match, tighten `matches()` on one of them.

3. **Parse into rows (`extract`)**  
   Implement **`extract(self, doc_text: str, filename: str) -> list[dict]`**.  
   Return **one dict per test case** (or logical row). Keys must align with what the app exports (see below).

4. **Ship it**  
   - Add a module under **`extractors/`** that subclasses **`BaseExtractor`** (`extractors/base.py`).  
   - Put a representative **`samples/your_format.docx`** and document it in **`samples/README.md`**.  
   - Restart the app; extractors are **auto-imported** (see `_load_extractors` in `extractors/__init__.py`).

## Output row shape (contract)

Each row dict should include these keys (strings; empty string if missing):

| Key | Meaning |
|-----|--------|
| `file_name` | Source filename (pass through `filename` argument). |
| `test_id` | Stable ID if the format has one; else empty or synthetic. |
| `test_name` | Human-readable title. |
| `description` | Free text. |
| `preconditions` | Preconditions / applicability. |
| `steps` | Steps or actions. |
| `expected_results` | Expected results / outcomes. |

The Excel exporter (`exporter.py`) expects exactly this set. Missing keys become blank cells.

## Given a new document — agent checklist

1. Save it as **`samples/<name>.docx`**.
2. Print **`read_docx`** output; note:
   - How test cases are **bounded** (e.g. `##` headings, numbered sections).
   - Where **IDs** live (heading suffix, table column, paragraph).
   - How **steps / expected** appear (one table vs two, column headers, merged cells flattened to text).
3. Draft **`matches`**: return `True` only when those signatures appear together.
4. Draft **`extract`**: split `doc_text` into sections; for each test case, fill the row dict.
5. Add **`name`** on the class (human-readable, shown in UI as the matched template).
6. Run the Flask **`POST /extract`** flow or the UI **Test case extractor** tab; confirm **`file_results`** shows **matched template** and sensible row counts.

## Reference implementation

- **`extractors/user_management.py`** — underscore-style TC IDs in `##` headings, tables for steps/expected, two layout variants.
- **`samples/README.md`** — short “how to add a format” blurb.

## When extraction returns zero rows but `matches` is true

- Template matched structurally but parsing found no sections — tighten **`extract`** (split logic, regex, table detection).
- Document differs slightly from the sample — extend parsing or narrow **`matches`** so false positives don’t hit this extractor.

## When nothing matches (`no_template`)

- No extractor’s **`matches`** returned `True`.  
- User-facing copy tells them to ask a developer for a **new module under `extractors/`** using **`samples/`** as reference — that’s this workflow.

## Non-goals (current design)

- **No merge of multiple extractors** on one file: exactly **one** profile applies per file attempt order.
- **No XML-level parsing** in extractors today: work from **`doc_text`** only unless you intentionally extend the reader.

---

*Keep this file updated when the row contract or discovery rules change.*
