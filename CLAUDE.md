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

**Bulk folder extraction (no UI):** see [`scripts/BULK_EXTRACT.md`](scripts/BULK_EXTRACT.md) and `scripts/folder_batch_extract.py` — processes a source directory into one `.xlsx` per document via `POST /extract` and `POST /download` (default: skip inputs whose output `.xlsx` already exists; transient HTTP retries with backoff).

**LLM section mode:** if one section’s model call fails, extraction **continues** with remaining sections and keeps rows from successful sections (failures are listed in **`errors`**). The Test case extractor UI retries transient **`/extract`** failures with the same backoff policy as the batch script.

## Environment variables (`.env`)

The app loads `.env` via `python-dotenv` on startup (optional file).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SUPPORT_UPLOAD_DIR` | No | `support_uploads` | **Save for developer** inbox (always on). Relative paths resolve from the folder containing `app.py`; omit `.env` entirely to use the default folder next to `app.py`. |
| `LOG_LEVEL` | No | `INFO` | Python logging level (`DEBUG`, `INFO`, …). |
| `LLM_STREAM` | No | `1` | When truthy, **whole-file** LLM extraction uses OpenAI-style **streaming** (`stream: true`) if the UI leaves streaming on Auto; set `0` / `false` / `off` for one-shot completions. |
| `LLM_STREAM_SECTIONS` | No | `0` | When truthy, **section-by-section** extraction streams each chunk; default off (one JSON body per section). UI **Always stream** overrides. |
| `LLM_IO_LOG_PATH` | No | *(unset)* | If set, append JSON lines for each LLM **chat** request and response to this file ( **`Authorization` redacted** ). Relative paths are resolved from the project root next to `app.py`. Contains document text and model output — protect the file. |

## LLM extraction (UI)

On the **Test case extractor** tab you can choose **LLM (OpenAI-compatible)** instead of template extractors.

- **Per request only:** base URL, API key, and model are sent with `POST /extract` as form fields; listing models uses `POST /llm-models` with JSON. They are **not** written to `.env`, disk, or logs (do not enable logging of raw multipart bodies in production).
- **HTTPS:** if the app is not on `localhost`, use HTTPS so the key is not sent in clear text.
- **Ollama:** use **Ollama · localhost** when Flask runs on your machine (`http://127.0.0.1:11434/v1`), or **Ollama · Docker host** when the app runs in Docker and Ollama is on the host (`http://host.docker.internal:11434/v1`). API key `ollama`, then pick a model (optional **Fetch models**). Linux Docker may need `--add-host=host.docker.internal:host-gateway` if `host.docker.internal` is missing.
- **Streaming:** UI **Streaming (SSE)** — **Auto** (omit form field `llm_stream`) uses **`LLM_STREAM`** for whole-file extract (default on) and **`LLM_STREAM_SECTIONS`** for section mode (default off). **Always stream** / **Never stream** send `llm_stream=1` or `0`. If streaming returns empty content, the server falls back to a non-streaming completion.
- **Debug file:** set **`LLM_IO_LOG_PATH`** (e.g. `llm_io.log`) to append structured request/response records for **extract** calls only (not `/llm-models`). API keys are not written verbatim (`Bearer <redacted>`).
- **Section mode:** **`llm_document_scope`** (`whole` \| `sections`; **default `sections`** in UI and `/extract`). When `sections`, also **`llm_section_split`**: **`headings`** (default) uses **`llm_heading_level`** (`auto` \| `1`–`6`) on markdown heading lines (`#` … `######`; **Auto** = shallowest level present); **`patterns`** uses **`llm_section_regex_hints`** — one Python regex per line (comment lines start with `#`), each match on a trimmed line starts a new section — useful when titles carry ids like `x_y_z`. Regex mode requires non-empty hints; if nothing matches, the whole file is one section (warning in logs). Optional **`llm_user_hints`** (short text, capped server-side) is prefixed into the model prompt for id/title conventions. PDFs often lack `#` lines — try regex split or whole file.
- Output rows match [`exporter.py`](exporter.py) columns (including **`procedure_steps`** and **`expected_results`** for LLM). Implementation: [`llm_extractor.py`](llm_extractor.py). Template extractors may still emit legacy **`steps_expected`** until updated — that key is not a workbook column; **Procedure** and **Expected** cells stay empty for template mode until those extractors are migrated.

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

5. **`llm_extractor.py`** — optional OpenAI-compatible `chat/completions` path; strict JSON `test_cases` → normalized row dicts.

6. **`app.py`** — Flask routes include:
   - `GET /` — UI
   - `GET /health` — status, extractor list; **`support_upload_enabled`** is always true (inbox uses `SUPPORT_UPLOAD_DIR` or default `support_uploads`)
   - `POST /preview-doc` — single `.docx` or `.pdf` → parsed text preview
   - `POST /llm-models` — JSON `{ llm_base_url, llm_api_key? }` → `{ models: [...] }` via OpenAI-compatible `GET …/models` or Ollama `GET …/api/tags` (server-side; credentials not persisted). UI **Fetch models** uses this.
   - `POST /support-upload` — saves one `.docx`/`.pdf` into `SUPPORT_UPLOAD_DIR` (default `./support_uploads`) with a unique filename; returns `{ ok, reference }` (inbox is gitignored)
   - `POST /extract` — multipart uploads → combined rows + per-file `file_results`; form field `mode=template` (default) or `mode=llm` with `llm_base_url`, `llm_api_key`, `llm_model`; optional `llm_stream` (`1`|`0`) forces SSE on/off (omit for Auto: whole-file vs section defaults from env).
   - `POST /download` — `{rows}` JSON → `.xlsx`
   - `GET /samples/<path>` — static sample files
   - Excel shrinker routes under `/excel/…`

7. **`exporter.py`** — builds the workbook from row dicts (`openpyxl`). Columns include **`procedure_steps`** and **`expected_results`**. Template-based rows may omit them until extractors are updated.

The frontend (`templates/index.html`) is a single self-contained HTML file with vanilla JS — no build step.
