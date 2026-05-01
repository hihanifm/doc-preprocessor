# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

**Doc Clinic** — local Flask app for fixing up office files: extract structured rows (incl. test cases) from `.docx` via template extractors, filter and preview in the UI, export `.xlsx`, and shrink huge spreadsheets with column filters before download.

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

The pipeline is linear — one request, four steps:

1. **`readers/docx_reader.py`** — converts `.docx` to structured plain text. Headings → `## text`, tables → pipe-delimited rows. Preserves document order via `_iter_block_items` (walks raw XML to interleave paragraphs and tables correctly, since `python-docx` doesn't do this natively).

2. **`llm_extractor.py`** — sends the plain text to an OpenAI-compatible chat endpoint. The system prompt instructs the model to return a JSON object with a `test_cases` array. Falls back from `json_object` response format to plain completion if the model returns 400/422 (handles older Ollama models). Attaches `file_name` to each extracted row.

3. **`app.py`** — Flask app with three routes:
   - `GET /` — serves the UI
   - `POST /extract` — accepts multipart file uploads, runs the pipeline per file, returns JSON `{rows, errors}`
   - `POST /download` — accepts `{rows}` JSON, calls `to_excel`, streams back `.xlsx`
   - `GET /config` — returns masked LLM config + live connection check

4. **`exporter.py`** — converts the list of row dicts to an `openpyxl` workbook with styled headers, wrapped text, and frozen pane. Returns raw bytes (no temp files).

The frontend (`templates/index.html`) is a single self-contained HTML file with vanilla JS — no build step. It calls `/extract` then `/download` to complete the flow client-side.
