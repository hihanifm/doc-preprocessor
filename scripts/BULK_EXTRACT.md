# Bulk folder extraction (no UI)

Run on the **same machine as Docs Garage** (e.g. SSH into the lab), with Flask already listening on `--base-url` (default `http://127.0.0.1:35050`). Traffic stays on loopback; no browser required.

## Usage

```bash
source .venv/bin/activate
python scripts/folder_batch_extract.py \
  --source /path/to/indir \
  --output /path/to/outdir \
  --base-url http://127.0.0.1:35050 \
  --mode template
```

- On start, the script prints a **`[bulk]`** banner to **stderr** (resolved `POST` URLs, mode, retries, `NO_PROXY` hint if proxy env vars are set) so you can confirm the target server before work begins.
- Processes **one file per HTTP request** (each `.docx` / `.pdf` → one `.xlsx`).
- **Non-recursive** by default; use `--recursive` for subfolders.
- Writes **`Stem.xlsx`** next to each basename; use **`--disambiguate-ext`** if the same stem exists as both `.docx` and `.pdf` (`Stem_docx.xlsx`, `Stem_pdf.xlsx`).
- **LLM mode (default):** omits **`llm_progress_stream`** so the server uses **`1`** — same as the web UI: **NDJSON progress** (section starts/done, failures, row counts) prints to stderr as `llm: …` lines while the run completes. Use **`--no-llm-progress-stream`** for a single JSON response (no live section lines).

### Retries (transient errors)

- **`POST /extract`** and **`POST /download`** retry up to **`--max-retries`** times (default **10**) with waits **5, 10, 20, 40, 80, 160, 320, 640** seconds (then **640s**). Use **`--no-retry`** for a single attempt (debugging).
- Retries apply to: **`URLError`** / timeouts, **`JSONDecodeError`** on a success response body, HTTP **408, 429, 500, 502, 503, 504**. Validation **4xx** (except 408/429) are **not** retried.

### Skip existing outputs (default resume)

- By default, if **`Stem.xlsx`** (or disambiguated name) already exists under **`--output`**, that document is **skipped** — reruns continue where the last batch stopped without redoing LLM work. Use **`--force`** to always re-extract and overwrite.

### LLM section mode and partial rows

- Same behavior as the web UI: if **one section** fails in LLM section mode, the server **still returns rows from successful sections** and adds messages to **`errors`**. You can still get a workbook whenever **`rows`** is non-empty.

### Template mode

```bash
python scripts/folder_batch_extract.py --source ./in --output ./out --mode template
```

### LLM mode

Required: `--llm-base-url`, `--llm-model`, and usually `--llm-api-key` (`ollama` for local Ollama).

```bash
python scripts/folder_batch_extract.py \
  --source ./in --output ./out \
  --mode llm \
  --llm-base-url http://127.0.0.1:11434/v1 \
  --llm-api-key ollama \
  --llm-model llama3.2 \
  --llm-document-scope sections \
  --llm-heading-level auto
```

If `--llm-section-split patterns`, provide **`--llm-section-regex-hints`** (inline text or path to a text file).

### Exit codes and logs

- **`--fail-fast`**: stop after the first error.
- **`--strict-exit`**: exit status **1** if any extract/download failure was logged (`file_results.ok == false`, HTTP errors, etc.).
- **`--skip-empty-rows`**: treat **zero rows** as a failure (logged + counts toward **`--strict-exit`**).
- Failures append JSON lines to **`--log-file`** (default: `<output>/batch_extract_failures.log`).

### Environment defaults (optional)

| Env | Maps to |
|-----|---------|
| `DOCS_GARAGE_URL` | `--base-url` |
| `DOCS_GARAGE_MODE` | `--mode` (`template` or `llm`) |

CLI flags override env.

### Limits

- Same **`MAX_CONTENT_LENGTH`** as the server (**50 MB** per upload).
- LLM **`LLM_RPM`** and model latency apply per document.

## API mapping

Form fields mirror **`POST /extract`** in [`app.py`](../app.py): `mode`, `files`, LLM fields, and **`llm_progress_stream=0`** for scripts. Successful **`rows`** are sent to **`POST /download`** as `{"rows": [...]}`.
