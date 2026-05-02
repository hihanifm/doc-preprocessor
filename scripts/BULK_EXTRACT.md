# Bulk folder extraction (no UI)

Run on the **same machine as Docs Garage** (e.g. SSH into the lab), with Flask already listening on `--base-url` (default `http://127.0.0.1:5000`). Traffic stays on loopback; no browser required.

## Usage

```bash
source .venv/bin/activate
python scripts/folder_batch_extract.py \
  --source /path/to/indir \
  --output /path/to/outdir \
  --base-url http://127.0.0.1:5000 \
  --mode template
```

- Processes **one file per HTTP request** (each `.docx` / `.pdf` → one `.xlsx`).
- **Non-recursive** by default; use `--recursive` for subfolders.
- Writes **`Stem.xlsx`** next to each basename; use **`--disambiguate-ext`** if the same stem exists as both `.docx` and `.pdf` (`Stem_docx.xlsx`, `Stem_pdf.xlsx`).
- **LLM mode** sends **`llm_progress_stream=0`** so the API returns a single JSON body (not NDJSON).

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
