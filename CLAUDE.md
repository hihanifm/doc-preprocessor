# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

**Docs Garage** — local Flask app for tinkering with office files in one place: extract structured rows (including test cases) from **`.docx`** or **digital `.pdf`** via template extractors, filter and preview in the UI, export `.xlsx`, and shrink huge spreadsheets with column filters before download.

**PDF notes:** Only PDFs with an **extractable text layer** are supported. **OCR is not supported** (scanned PDFs usually yield no text). Tables are detected heuristically (`pdfplumber`) and rendered as pipe-delimited lines similar to Word output; messy layouts may need PDF-specific extractors or reader tuning.

## Running the app

```bash
# First-time setup: copy and fill in .env
cp .env.example .env

# Start (creates venv, installs deps, launches server on http://localhost:5000)
./start.sh

# Or manually
source .venv/bin/activate
python app.py [--host 0.0.0.0] [--port 5000]
```

## Environment variables (`.env`)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `LLM_API_KEY` | Yes | — | API key (`"ollama"` for local Ollama) |
| `LLM_BASE_URL` | No | OpenAI | Override endpoint (e.g. `http://localhost:11434/v1`) |
| `LLM_MODEL` | No | `gpt-4o` | Model name |

## Installing dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Architecture

The document pipeline is linear — upload → normalize to plain text → pick extractor → rows → Excel:

1. **`readers/docx_reader.py`** — converts `.docx` to structured plain text. Headings → `#` / `##`, tables → pipe-delimited rows. Preserves document order via `_iter_block_items`.

2. **`readers/pdf_reader.py`** — converts digital `.pdf` to plain text with **`pdfplumber`**: page text and **detected tables** are interleaved top-to-bottom; tables use the same ` | ` cell spacing style as Word output.

3. **`readers/document_reader.py`** — `read_document(path)` dispatches on extension (`.docx` vs `.pdf`).

4. **`extractors/`** — template modules implement `matches(doc_text)` and `extract(doc_text, filename)`; the first match wins (`extractors/__init__.py`).

5. **`app.py`** — Flask routes include:
   - `GET /` — UI
   - `POST /preview-doc` — single `.docx` or `.pdf` → parsed text preview
   - `POST /extract` — multipart uploads → combined rows + per-file `file_results`
   - `POST /download` — `{rows}` JSON → `.xlsx`
   - `GET /samples/<path>` — static sample files
   - Excel shrinker routes under `/excel/…`

6. **`exporter.py`** — builds the workbook from row dicts (`openpyxl`).

The frontend (`templates/index.html`) is a single self-contained HTML file with vanilla JS — no build step.
