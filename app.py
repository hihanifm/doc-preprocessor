import logging
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response, send_from_directory

import extractors as extractor_registry

load_dotenv()


def _configure_logging() -> None:
    if logging.root.handlers:
        return
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


_configure_logging()
logger = logging.getLogger(__name__)

from excel_filter import (
    filter_xlsx_to_bytes,
    peek_distinct,
    sample_sheet_rows,
    workbook_sheet_info,
)
from exporter import to_excel
from llm_extractor import LlmExtractError, extract_with_llm, fetch_model_ids, validate_llm_form
from readers.document_reader import read_document


def _document_suffix(filename: str | None) -> str | None:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in (".docx", ".pdf") else None


_DEFAULT_SUPPORT_UPLOAD_DIR = "support_uploads"


def _support_upload_dir_resolved() -> str:
    raw = (os.environ.get("SUPPORT_UPLOAD_DIR") or "").strip()
    rel = raw if raw else _DEFAULT_SUPPORT_UPLOAD_DIR
    path = rel if os.path.isabs(rel) else os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)
    return os.path.normpath(path)


def _safe_support_save_parts(original: str | None) -> tuple[str, str] | None:
    base = os.path.basename(original or "")
    if not base or base in (".", "..") or ".." in base.replace("\\", "/"):
        return None
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in (".docx", ".pdf"):
        return None
    stem_clean = re.sub(r"[^\w\-.]+", "_", stem, flags=re.UNICODE).strip("._-")[:120]
    if not stem_clean:
        stem_clean = "document"
    return stem_clean, ext


FILTER_MODES = frozenset({"contains", "equals", "not_contains", "starts_with"})

_MAX_LLM_USER_HINT_CHARS = 8000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


@app.route("/health")
def health():
    exts = [e.name for e in extractor_registry.get_extractors()]
    return jsonify({
        "status": "ok",
        "commit": _git_commit(),
        "extractors": exts,
        "extractor_count": len(exts),
        "support_upload_enabled": True,
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extractors")
def list_extractors():
    return jsonify({"extractors": [e.name for e in extractor_registry.get_extractors()]})


@app.route("/preview-doc", methods=["POST"])
def preview_doc():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    suffix = _document_suffix(f.filename)
    if not suffix:
        return jsonify({"error": "Upload a .docx or .pdf file"}), 400
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        doc_text = read_document(tmp_path)
        return jsonify({"document_text": doc_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


@app.route("/llm-models", methods=["POST"])
def llm_models():
    """List model IDs from an OpenAI-compatible GET /v1/models or Ollama /api/tags (server-side only)."""
    if not request.is_json:
        return jsonify({"error": "Send JSON with Content-Type application/json: llm_base_url, llm_api_key (optional)."}), 400
    body = request.get_json(silent=True) or {}
    llm_base_url = (body.get("llm_base_url") or "").strip()
    llm_api_key = (body.get("llm_api_key") or "").strip()
    if not llm_base_url:
        return jsonify({"error": "llm_base_url is required."}), 400
    logger.info(
        "POST /llm-models base_url=%r api_key_provided=%s",
        llm_base_url,
        bool(llm_api_key),
    )
    try:
        models = fetch_model_ids(llm_base_url, llm_api_key)
        logger.info("POST /llm-models ok model_count=%d", len(models))
        return jsonify({"models": models})
    except LlmExtractError as e:
        logger.warning("POST /llm-models failed: %s", e)
        return jsonify({"error": str(e)}), 400


@app.route("/support-upload", methods=["POST"])
def support_upload():
    """Save one .docx/.pdf under SUPPORT_UPLOAD_DIR (default: ./support_uploads)."""
    out_dir = _support_upload_dir_resolved()

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400

    parts = _safe_support_save_parts(f.filename)
    if not parts:
        return jsonify({"error": "Upload a .docx or .pdf with a valid filename."}), 400

    stem_clean, ext = parts
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:8]
    reference = f"{ts}_{stem_clean}_{short}{ext}"
    dest_path = os.path.join(out_dir, reference)

    try:
        os.makedirs(out_dir, exist_ok=True)
        f.save(dest_path)
    except OSError as e:
        return jsonify({"error": f"Could not save file: {e}"}), 500

    return jsonify({"ok": True, "reference": reference})


@app.route("/extract", methods=["POST"])
def extract():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    mode = (request.form.get("mode") or "template").strip().lower()
    llm_base_url = ""
    llm_api_key = ""
    llm_model = ""
    llm_document_scope = "whole"
    llm_heading_level = "auto"
    llm_section_split = "headings"
    llm_section_regex_hints = ""
    llm_user_hints = ""
    llm_stream: bool | None = None
    if mode == "llm":
        llm_base_url = request.form.get("llm_base_url", "").strip()
        llm_api_key = request.form.get("llm_api_key", "").strip()
        llm_model = request.form.get("llm_model", "").strip()
        form_err = validate_llm_form(llm_base_url, llm_api_key, llm_model)
        if form_err:
            return jsonify({"error": form_err}), 400
        llm_document_scope = (request.form.get("llm_document_scope") or "whole").strip().lower()
        if llm_document_scope not in ("whole", "sections"):
            return jsonify({"error": "llm_document_scope must be whole or sections."}), 400
        llm_heading_level = (request.form.get("llm_heading_level") or "auto").strip().lower()
        if llm_heading_level not in ("auto", "1", "2", "3", "4", "5", "6"):
            return jsonify({"error": "llm_heading_level must be auto or 1–6."}), 400
        llm_section_split = (request.form.get("llm_section_split") or "headings").strip().lower()
        if llm_section_split not in ("headings", "patterns"):
            return jsonify({"error": "llm_section_split must be headings or patterns."}), 400
        llm_section_regex_hints = request.form.get("llm_section_regex_hints") or ""
        llm_user_hints = (request.form.get("llm_user_hints") or "").strip()
        if len(llm_user_hints) > _MAX_LLM_USER_HINT_CHARS:
            return jsonify(
                {"error": f"Optional hints are too long (max {_MAX_LLM_USER_HINT_CHARS} characters)."}
            ), 400
        if llm_document_scope == "sections" and llm_section_split == "patterns":
            if not llm_section_regex_hints.strip():
                return jsonify(
                    {
                        "error": "Enter at least one regex line for section patterns, or switch split to "
                        "Markdown headings."
                    }
                ), 400
        raw_llm_stream = (request.form.get("llm_stream") or "").strip().lower()
        if raw_llm_stream in ("1", "true", "yes", "on"):
            llm_stream = True
        elif raw_llm_stream in ("0", "false", "no", "off"):
            llm_stream = False
        # empty / auto → None (server picks by scope: whole uses LLM_STREAM; sections use LLM_STREAM_SECTIONS)

    all_rows = []
    errors = []
    file_results: list[dict] = []
    templates_order: list[str] = []
    tmp_path = None

    for f in files:
        display_name = f.filename or "(upload)"
        suffix = _document_suffix(f.filename)
        if not suffix:
            msg = f"{display_name}: only .docx and .pdf files are supported"
            errors.append(msg)
            file_results.append(
                {
                    "filename": display_name,
                    "template": None,
                    "rows": 0,
                    "ok": False,
                    "reason": "unsupported",
                }
            )
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                f.save(tmp.name)
                tmp_path = tmp.name

            doc_text = read_document(tmp_path)
            if not doc_text.strip():
                errors.append(
                    f"{display_name}: No extractable text in this PDF (common for scanned "
                    "documents). OCR is not supported — use a digital/text-based PDF."
                )
                file_results.append(
                    {
                        "filename": display_name,
                        "template": None,
                        "rows": 0,
                        "ok": False,
                        "reason": "empty_text",
                    }
                )
                continue

            if mode == "llm":
                tpl_label = f"LLM ({llm_model})"
                try:
                    rows, llm_doc_meta = extract_with_llm(
                        doc_text,
                        base_url=llm_base_url,
                        api_key=llm_api_key,
                        model=llm_model,
                        file_name=display_name,
                        document_scope=llm_document_scope,
                        heading_level=llm_heading_level,
                        section_split=llm_section_split,
                        section_regex_hints=llm_section_regex_hints,
                        user_hints=llm_user_hints,
                        stream=llm_stream,
                    )
                    all_rows.extend(rows)
                    if tpl_label not in templates_order:
                        templates_order.append(tpl_label)
                    fr_ok: dict = {
                        "filename": display_name,
                        "template": tpl_label,
                        "rows": len(rows),
                        "ok": True,
                        "llm_truncated": bool(llm_doc_meta.get("truncated")),
                        "llm_doc_chars": llm_doc_meta.get("doc_char_count"),
                        "llm_max_doc_chars": llm_doc_meta.get("max_doc_chars"),
                    }
                    if llm_doc_meta.get("llm_section_mode"):
                        fr_ok["llm_section_mode"] = True
                        fr_ok["llm_section_calls"] = llm_doc_meta.get("llm_section_calls")
                        fr_ok["llm_section_split"] = llm_doc_meta.get("llm_section_split")
                        if llm_doc_meta.get("llm_heading_level_used") is not None:
                            fr_ok["llm_heading_level_used"] = llm_doc_meta.get("llm_heading_level_used")
                        if llm_doc_meta.get("llm_pattern_count") is not None:
                            fr_ok["llm_pattern_count"] = llm_doc_meta.get("llm_pattern_count")
                    file_results.append(fr_ok)
                except LlmExtractError as le:
                    logger.warning("POST /extract LLM failed file=%r model=%r: %s", display_name, llm_model, le)
                    errors.append(f"{display_name}: {le}")
                    file_results.append(
                        {
                            "filename": display_name,
                            "template": None,
                            "rows": 0,
                            "ok": False,
                            "reason": "llm_error",
                            "detail": str(le),
                        }
                    )
                continue

            ext = extractor_registry.find_extractor(doc_text)

            if ext is None:
                errors.append(
                    f"{display_name}: No extractor template matched this document. "
                    "Ask your developer to add an extractor under extractors/ "
                    "(see samples/ for reference formats)."
                )
                file_results.append(
                    {
                        "filename": display_name,
                        "template": None,
                        "rows": 0,
                        "ok": False,
                        "reason": "no_template",
                    }
                )
                continue

            rows = ext.extract(doc_text, display_name)
            all_rows.extend(rows)
            if ext.name not in templates_order:
                templates_order.append(ext.name)
            file_results.append(
                {
                    "filename": display_name,
                    "template": ext.name,
                    "rows": len(rows),
                    "ok": True,
                }
            )

        except Exception as e:
            errors.append(f"{display_name}: {e}")
            file_results.append(
                {
                    "filename": display_name,
                    "template": None,
                    "rows": 0,
                    "ok": False,
                    "reason": "exception",
                    "detail": str(e),
                }
            )
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                tmp_path = None

    template_used = " · ".join(templates_order) if templates_order else None

    return jsonify(
        {
            "rows": all_rows,
            "errors": errors,
            "template": template_used,
            "file_results": file_results,
        }
    )


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json()
    if not data or "rows" not in data:
        return jsonify({"error": "No data provided"}), 400

    xlsx_bytes = to_excel(data["rows"])
    return Response(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=test_cases.xlsx"},
    )


def _cleanup_tmp(path) -> None:
    if path:
        try:
            os.unlink(path)
        except Exception:
            pass


@app.route("/excel/sheet-info", methods=["POST"])
def excel_sheet_info():
    """Return column headers and row count without listing cell values (large-sheet safe)."""
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Upload a .xlsx or .xlsm file"}), 400
    tmp_path = None
    try:
        sheet_index = int(request.form.get("sheet_index", 0))
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        headers, row_count, sheet_names = workbook_sheet_info(tmp_path, sheet_index)
        return jsonify(
            {"columns": headers, "row_count": row_count, "sheet_names": sheet_names}
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(tmp_path)


@app.route("/excel/sample-rows", methods=["POST"])
def excel_sample_rows():
    """First N data rows as JSON for a compact table preview (on demand only)."""
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Upload a .xlsx or .xlsm file"}), 400
    tmp_path = None
    try:
        sheet_index = int(request.form.get("sheet_index", 0))
        limit = min(max(int(request.form.get("limit", 15)), 1), 50)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        columns, rows = sample_sheet_rows(tmp_path, sheet_index, max_rows=limit)
        return jsonify({"columns": columns, "rows": rows})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(tmp_path)


@app.route("/excel/peek-column", methods=["POST"])
def excel_peek_column():
    """Optional: sample distinct values (capped) — only when user asks."""
    f = request.files.get("file")
    column = (request.form.get("column") or "").strip()
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Upload a .xlsx or .xlsm file"}), 400
    if not column:
        return jsonify({"error": "column is required"}), 400
    tmp_path = None
    try:
        sheet_index = int(request.form.get("sheet_index", 0))
        max_values = min(int(request.form.get("max_values", 30)), 200)
        max_scan = min(int(request.form.get("max_scan", 20000)), 500000)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        values, truncated, scanned = peek_distinct(
            tmp_path,
            column,
            sheet_index=sheet_index,
            max_values=max_values,
            max_scan_rows=max_scan,
        )
        return jsonify(
            {"values": values, "truncated": truncated, "scanned_rows": scanned}
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(tmp_path)


@app.route("/excel/download-filtered", methods=["POST"])
def excel_download_filtered():
    """Shrink a spreadsheet by one column filter into a new workbook."""
    f = request.files.get("file")
    column = (request.form.get("column") or "").strip()
    mode = (request.form.get("mode") or "contains").strip().lower()
    needle = request.form.get("value") or ""
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Upload a .xlsx or .xlsm file"}), 400
    if not column:
        return jsonify({"error": "column is required"}), 400
    if mode not in FILTER_MODES:
        return jsonify({"error": f"mode must be one of: {', '.join(sorted(FILTER_MODES))}"}), 400
    tmp_path = None
    try:
        sheet_index = int(request.form.get("sheet_index", 0))
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        xlsx_bytes = filter_xlsx_to_bytes(tmp_path, column, mode, needle, sheet_index)
        base = os.path.splitext(f.filename)[0] or "filtered"
        out_name = f"{base}_filtered.xlsx"
        return Response(
            xlsx_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={out_name}"},
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(tmp_path)


@app.route("/samples/<path:filename>")
def serve_sample(filename):
    return send_from_directory("samples", filename)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
