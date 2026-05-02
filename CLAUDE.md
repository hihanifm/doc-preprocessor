# CLAUDE.md

Project guidance for human developers, **Claude Code**, **Cursor**, and other coding agents working in this repository.

## What this project does

**Docs Garage** — local Flask app for tinkering with office files in one place: extract structured rows (including test cases) from **`.docx`** or **digital `.pdf`** via template extractors, filter and preview in the UI, export **`.xlsx`**, and shrink huge spreadsheets with column filters before download.

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

The app loads `.env` via `python-dotenv` on startup (optional file).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SUPPORT_UPLOAD_DIR` | No | `support_uploads` | **Save for developer** inbox (always on). Relative paths resolve from the folder containing `app.py`; omit `.env` entirely to use the default folder next to `app.py`. |
| `LLM_API_KEY` | Yes | — | API key (`"ollama"` for local Ollama) |
| `LLM_BASE_URL` | No | OpenAI | Override endpoint (e.g. `http://localhost:11434/v1`) |
| `LLM_MODEL` | No | `gpt-4o` | Model name |

## Installing dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

This repository does **not** check in a virtualenv (`.venv/` is gitignored). **Do not commit or push `.venv`** — it is machine-specific, large, and reproducible from [`requirements.txt`](requirements.txt).

**Agents and CI-style environments:** create `.venv` if missing, **activate** it, then use `pip` / `python` so dependencies install into the venv, not the system interpreter.

1. From the repo root: `python3 -m venv .venv` (or `python -m venv .venv` on Windows).
2. Activate, then install:
   - Unix/macOS: `source .venv/bin/activate && pip install -r requirements.txt`
   - Windows: run `.venv\Scripts\activate` then `pip install -r requirements.txt`

Prefer **`./start.sh`** (Unix) or **`start.bat`** / **`start_lan.bat`** (Windows) when you just need to run the app — they create `.venv` when needed and install deps first.

## Extractors

When adding or changing extractors, follow **[`extractors/SKILL.md`](extractors/SKILL.md)**.

## Architecture

The document pipeline is linear — upload → normalize to plain text → pick extractor → rows → Excel:

1. **`readers/docx_reader.py`** — converts `.docx` to structured plain text. Headings → `#` / `##`, tables → pipe-delimited rows. Preserves document order via `_iter_block_items`.

2. **`readers/pdf_reader.py`** — converts digital `.pdf` to plain text with **`pdfplumber`**: page text and **detected tables** are interleaved top-to-bottom; tables use the same ` | ` cell spacing style as Word output.

3. **`readers/document_reader.py`** — `read_document(path)` dispatches on extension (`.docx` vs `.pdf`).

4. **`extractors/`** — template modules implement `matches(doc_text)` and `extract(doc_text, filename)`; the first match wins (`extractors/__init__.py`).

5. **`app.py`** — Flask routes include:
   - `GET /` — UI
   - `GET /health` — status, extractor list; **`support_upload_enabled`** is always true (inbox uses `SUPPORT_UPLOAD_DIR` or default `support_uploads`)
   - `POST /preview-doc` — single `.docx` or `.pdf` → parsed text preview
   - `POST /support-upload` — saves one `.docx`/`.pdf` into `SUPPORT_UPLOAD_DIR` (default `./support_uploads`) with a unique filename; returns `{ ok, reference }` (inbox is gitignored)
   - `POST /extract` — multipart uploads → combined rows + per-file `file_results`
   - `POST /download` — `{rows}` JSON → `.xlsx`
   - `GET /samples/<path>` — static sample files
   - Excel shrinker routes under `/excel/…`

6. **`exporter.py`** — builds the workbook from row dicts (`openpyxl`). Default columns include **`steps_expected`** (flattened procedure-table text) instead of separate steps vs expected columns.

The frontend (`templates/index.html`) is a single self-contained HTML file with vanilla JS — no build step.
