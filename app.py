import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response, send_from_directory, stream_with_context

import extractors as extractor_registry

load_dotenv()


def _configure_logging() -> None:
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    if not logging.root.handlers:
        logging.basicConfig(level=level, format=fmt)
    else:
        # Gunicorn (and others) install root handlers before we import; still honor LOG_LEVEL.
        logging.getLogger().setLevel(level)
    logging.getLogger(__name__).setLevel(level)


_configure_logging()
logger = logging.getLogger(__name__)


class _ExtractRequestLogAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return "req_id=%s %s" % (self.extra.get("req_id", "-"), msg), kwargs


def _client_request_id() -> str | None:
    """Optional id from the client (batch script, curl) for log ↔ client correlation."""
    for key in ("X-Request-ID", "X-Request-Id", "X-Correlation-Id"):
        v = (request.headers.get(key) or "").strip()
        if v and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", v):
            return v[:64]
    return None


def _client_ip() -> str:
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return (request.remote_addr or "-").strip() or "-"


def _extract_reject(msg: str, *, req_id: str = "-"):
    """Log and return 400 JSON for /extract client / validation errors."""
    logger.warning("POST /extract 400 ip=%s req_id=%s detail=%s", _client_ip(), req_id, msg)
    r = jsonify({"error": msg})
    r.headers["X-Request-ID"] = req_id
    return r, 400


from excel_filter import (
    filter_xlsx_to_bytes,
    join_xlsx_to_bytes,
    merge_xlsx_to_bytes,
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


def _merge_contiguous_duplicates(rows):
    """Merge adjacent rows with the same test_id; flag non-contiguous duplicates."""
    TEXT_FIELDS = ("description", "preconditions", "procedure_steps", "expected_results")
    merged = []
    i = 0
    while i < len(rows):
        run = [rows[i]]
        tid = rows[i].get("test_id", "")
        while i + len(run) < len(rows) and rows[i + len(run)].get("test_id") == tid:
            run.append(rows[i + len(run)])
        if len(run) > 1 and tid:
            base = dict(run[0])
            for field in TEXT_FIELDS:
                parts = [r[field] for r in run if r.get(field, "").strip()]
                base[field] = "\n".join(parts)
            merged.append(base)
        else:
            merged.extend(run)
        i += len(run)

    seen: dict = {}
    for idx, row in enumerate(merged):
        tid = row.get("test_id", "")
        if tid:
            seen.setdefault(tid, []).append(idx)
    warnings = []
    for tid, positions in seen.items():
        if len(positions) > 1:
            warnings.append(f"⚠ test_id '{tid}' has non-contiguous duplicate rows — review required.")
            note = f"⚠ Duplicate test_id (non-contiguous): {tid}\n\n"
            for pos in positions:
                merged[pos]["description"] = note + merged[pos].get("description", "")
    return merged, warnings


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


def _cleanup_staged_path(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _stage_uploaded_files(files) -> list[dict[str, Any]]:
    """Save uploads to temp files. Each item is {\"kind\": \"ok\", ...} or {\"kind\": \"bad\", ...}."""
    out: list[dict[str, Any]] = []
    for f in files:
        display_name = f.filename or "(upload)"
        suffix = _document_suffix(f.filename)
        if not suffix:
            out.append({"kind": "bad", "display_name": display_name, "reason": "unsupported"})
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                f.save(tmp.name)
                out.append({"kind": "ok", "display_name": display_name, "path": tmp.name})
        except OSError as e:
            out.append(
                {
                    "kind": "bad",
                    "display_name": display_name,
                    "reason": "save_error",
                    "detail": str(e),
                }
            )
    return out


def _extract_core(
    work_list: list[dict[str, Any]],
    *,
    mode: str,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    llm_document_scope: str,
    llm_heading_level: str,
    llm_section_split: str,
    llm_section_regex_hints: str,
    llm_user_hints: str,
    llm_stream: bool | None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    all_rows: list = []
    errors: list[str] = []
    file_results: list[dict] = []
    templates_order: list[str] = []
    n_ok = sum(1 for w in work_list if w.get("kind") == "ok")
    fi = 0

    for item in work_list:
        if item.get("kind") == "bad":
            display_name = item["display_name"]
            if item.get("reason") == "unsupported":
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
            else:
                detail = item.get("detail") or "could not save upload"
                msg = f"{display_name}: {detail}"
                errors.append(msg)
                file_results.append(
                    {
                        "filename": display_name,
                        "template": None,
                        "rows": 0,
                        "ok": False,
                        "reason": "exception",
                        "detail": detail,
                    }
                )
            continue

        fi += 1
        display_name = item["display_name"]
        tmp_path = item["path"]

        if progress:
            progress(
                {
                    "step": "file_begin",
                    "file": display_name,
                    "index": fi,
                    "total_files": n_ok,
                }
            )

        try:
            _size_kb = os.path.getsize(tmp_path) // 1024
            logger.info("reading %s (%d KB)", display_name, _size_kb)
            _t0 = time.monotonic()
            doc_text = read_document(tmp_path)
            logger.info("read done %s elapsed=%.1fs chars=%d", display_name, time.monotonic() - _t0, len(doc_text))
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
                    progress=progress,
                )
                all_rows.extend(rows)
                sect_fail = llm_doc_meta.get("llm_section_failures") or []
                for sf in sect_fail:
                    errors.append(f"{display_name}: {sf}")
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
                if sect_fail:
                    fr_ok["llm_section_failure_count"] = len(sect_fail)
                if llm_doc_meta.get("llm_section_mode"):
                    fr_ok["llm_section_mode"] = True
                    fr_ok["llm_section_calls"] = llm_doc_meta.get("llm_section_calls")
                    fr_ok["llm_section_split"] = llm_doc_meta.get("llm_section_split")
                    if llm_doc_meta.get("llm_heading_level_used") is not None:
                        fr_ok["llm_heading_level_used"] = llm_doc_meta.get("llm_heading_level_used")
                    if llm_doc_meta.get("llm_pattern_count") is not None:
                        fr_ok["llm_pattern_count"] = llm_doc_meta.get("llm_pattern_count")
                    if llm_doc_meta.get("llm_prep_removed_headings"):
                        fr_ok["llm_prep_removed_headings"] = llm_doc_meta.get("llm_prep_removed_headings")
                    if llm_doc_meta.get("llm_prep_fallback_original"):
                        fr_ok["llm_prep_fallback_original"] = True
                    if "llm_rpm" in llm_doc_meta:
                        fr_ok["llm_rpm"] = llm_doc_meta.get("llm_rpm")
                    if llm_doc_meta.get("llm_section_empty_vz_tc_placeholder_count"):
                        fr_ok["llm_section_empty_vz_tc_placeholder_count"] = llm_doc_meta.get(
                            "llm_section_empty_vz_tc_placeholder_count"
                        )
                file_results.append(fr_ok)
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

        except LlmExtractError as le:
            logger.warning(
                "POST /extract LLM failed file=%r model=%r: %s",
                display_name,
                llm_model,
                le,
            )
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
        except Exception as e:
            logger.exception("extraction failed: %s", display_name)
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
            _cleanup_staged_path(tmp_path)

    template_used = " · ".join(templates_order) if templates_order else None

    all_rows, dup_warnings = _merge_contiguous_duplicates(all_rows)
    errors.extend(dup_warnings)

    return {
        "rows": all_rows,
        "errors": errors,
        "template": template_used,
        "file_results": file_results,
    }


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
        _size_kb = os.path.getsize(tmp_path) // 1024
        logger.info("reading %s (%d KB)", f.filename, _size_kb)
        _t0 = time.monotonic()
        doc_text = read_document(tmp_path)
        logger.info("read done %s elapsed=%.1fs chars=%d", f.filename, time.monotonic() - _t0, len(doc_text))
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
    _hdr_id = _client_request_id()
    req_id = _hdr_id or uuid.uuid4().hex[:12]
    log = _ExtractRequestLogAdapter(logger, {"req_id": req_id})
    _cl = request.content_length
    _ct = (request.content_type or "")[:200]
    log.info(
        "POST /extract begin ip=%s content_length=%s content_type=%r werkzeug_files_keys=%s form_field_keys=%s",
        _client_ip(),
        _cl if _cl is not None else "?",
        _ct,
        list(request.files.keys()),
        sorted(request.form.keys()),
    )

    files = request.files.getlist("files")
    fl_single = request.files.get("file")
    if not files:
        one = fl_single
        if one and (one.filename or "").strip():
            files = [one]
    if not files:
        log.warning(
            "POST /extract 400 no_file_parts ip=%s content_type=%r content_length=%s "
            "werkzeug_files_keys=%r getlist_files_names=%r get_file_name=%r",
            _client_ip(),
            _ct,
            _cl,
            list(request.files.keys()),
            [getattr(x, "filename", None) for x in request.files.getlist("files")],
            getattr(fl_single, "filename", None) if fl_single else None,
        )
        r = jsonify({"error": "No files uploaded"})
        r.headers["X-Request-ID"] = req_id
        return r, 400

    mode = (request.form.get("mode") or "template").strip().lower()
    upload_names = [(f.filename or "")[:200] for f in files]
    log.info(
        "POST /extract ip=%s mode=%s n_file_parts=%d upload_names=%s",
        _client_ip(),
        mode,
        len(files),
        upload_names,
    )
    llm_base_url = ""
    llm_api_key = ""
    llm_model = ""
    llm_document_scope = "sections"
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
            return _extract_reject(form_err, req_id=req_id)
        llm_document_scope = (request.form.get("llm_document_scope") or "sections").strip().lower()
        if llm_document_scope not in ("whole", "sections"):
            return _extract_reject("llm_document_scope must be whole or sections.", req_id=req_id)
        llm_heading_level = (request.form.get("llm_heading_level") or "auto").strip().lower()
        if llm_heading_level not in ("auto", "1", "2", "3", "4", "5", "6"):
            return _extract_reject("llm_heading_level must be auto or 1–6.", req_id=req_id)
        llm_section_split = (request.form.get("llm_section_split") or "headings").strip().lower()
        if llm_section_split not in ("headings", "patterns"):
            return _extract_reject("llm_section_split must be headings or patterns.", req_id=req_id)
        llm_section_regex_hints = request.form.get("llm_section_regex_hints") or ""
        llm_user_hints = (request.form.get("llm_user_hints") or "").strip()
        if len(llm_user_hints) > _MAX_LLM_USER_HINT_CHARS:
            return _extract_reject(
                f"Optional hints are too long (max {_MAX_LLM_USER_HINT_CHARS} characters).",
                req_id=req_id,
            )
        if llm_document_scope == "sections" and llm_section_split == "patterns":
            if not llm_section_regex_hints.strip():
                return _extract_reject(
                    "Enter at least one regex line for section patterns, or switch split to "
                    "Markdown headings.",
                    req_id=req_id,
                )
        raw_llm_stream = (request.form.get("llm_stream") or "").strip().lower()
        if raw_llm_stream in ("1", "true", "yes", "on"):
            llm_stream = True
        elif raw_llm_stream in ("0", "false", "no", "off"):
            llm_stream = False
        # empty / auto → None (server picks by scope: whole uses LLM_STREAM; sections use LLM_STREAM_SECTIONS)

    raw_prog = (request.form.get("llm_progress_stream") or "1").strip().lower()
    want_stream = mode == "llm" and raw_prog not in ("0", "false", "no", "off")

    work_list = _stage_uploaded_files(files)
    n_staged_ok = sum(1 for w in work_list if w.get("kind") == "ok")
    n_staged_bad = sum(1 for w in work_list if w.get("kind") == "bad")
    if mode == "llm":
        _bu = (llm_base_url or "")[:120]
        log.info(
            "POST /extract staged ip=%s mode=llm staged_ok=%d staged_bad=%d ndjson=%s model=%r "
            "llm_base_url_prefix=%r scope=%s split=%s",
            _client_ip(),
            n_staged_ok,
            n_staged_bad,
            want_stream,
            llm_model,
            _bu,
            llm_document_scope,
            llm_section_split,
        )
    else:
        log.info(
            "POST /extract staged ip=%s mode=template staged_ok=%d staged_bad=%d",
            _client_ip(),
            n_staged_ok,
            n_staged_bad,
        )

    if want_stream:
        q: queue.Queue[tuple[str, Any]] = queue.Queue()
        holder: dict[str, Any] = {}

        def worker():
            try:
                holder["payload"] = _extract_core(
                    work_list,
                    mode=mode,
                    llm_base_url=llm_base_url,
                    llm_api_key=llm_api_key,
                    llm_model=llm_model,
                    llm_document_scope=llm_document_scope,
                    llm_heading_level=llm_heading_level,
                    llm_section_split=llm_section_split,
                    llm_section_regex_hints=llm_section_regex_hints,
                    llm_user_hints=llm_user_hints,
                    llm_stream=llm_stream,
                    progress=lambda ev: q.put(("p", ev)),
                )
            except Exception as e:
                log.exception("POST /extract ndjson worker failed ip=%s", _client_ip())
                holder["err"] = e
            finally:
                q.put(("done", None))

        th = threading.Thread(target=worker, daemon=True)
        th.start()

        def generate():
            while True:
                kind, payload = q.get()
                if kind == "p":
                    yield (
                        json.dumps({"type": "progress", "data": payload}, ensure_ascii=False) + "\n"
                    ).encode("utf-8")
                elif kind == "done":
                    break
            th.join(timeout=720)
            err = holder.get("err")
            if err is not None:
                yield (json.dumps({"type": "error", "message": str(err)}, ensure_ascii=False) + "\n").encode(
                    "utf-8"
                )
                return
            pl = holder.get("payload")
            if pl is None:
                log.warning("POST /extract ndjson no payload after worker ip=%s", _client_ip())
                yield (
                    json.dumps({"type": "error", "message": "No extraction result"}, ensure_ascii=False) + "\n"
                ).encode("utf-8")
                return
            log.info(
                "POST /extract ndjson done ip=%s rows=%d errors=%d file_results=%d",
                _client_ip(),
                len(pl.get("rows") or []),
                len(pl.get("errors") or []),
                len(pl.get("file_results") or []),
            )
            yield (json.dumps({"type": "result", **pl}, ensure_ascii=False) + "\n").encode("utf-8")

        return Response(
            stream_with_context(generate()),
            mimetype="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Extract-Stream": "1",
                "X-Request-ID": req_id,
            },
        )

    out = _extract_core(
        work_list,
        mode=mode,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_document_scope=llm_document_scope,
        llm_heading_level=llm_heading_level,
        llm_section_split=llm_section_split,
        llm_section_regex_hints=llm_section_regex_hints,
        llm_user_hints=llm_user_hints,
        llm_stream=llm_stream,
        progress=None,
    )
    log.info(
        "POST /extract done ip=%s mode=%s rows=%d errors=%d file_results=%d",
        _client_ip(),
        mode,
        len(out.get("rows") or []),
        len(out.get("errors") or []),
        len(out.get("file_results") or []),
    )
    r = jsonify(out)
    r.headers["X-Request-ID"] = req_id
    return r


@app.route("/download", methods=["POST"])
def download():
    dl_rid = _client_request_id()
    data = request.get_json()
    if not data or "rows" not in data:
        logger.warning(
            "POST /download 400 ip=%s missing_json_or_rows request_id=%s",
            _client_ip(),
            dl_rid or "-",
        )
        r = jsonify({"error": "No data provided"})
        if dl_rid:
            r.headers["X-Request-ID"] = dl_rid
        return r, 400

    rows = data["rows"]
    nrows = len(rows) if isinstance(rows, list) else -1
    logger.info(
        "POST /download ip=%s rows=%s request_id=%s",
        _client_ip(),
        nrows,
        dl_rid or "-",
    )
    xlsx_bytes = to_excel(rows)
    hdrs: dict[str, str] = {"Content-Disposition": "attachment; filename=test_cases.xlsx"}
    if dl_rid:
        hdrs["X-Request-ID"] = dl_rid
    return Response(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=hdrs,
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


@app.route("/excel/merge", methods=["POST"])
def excel_merge():
    """Merge multiple .xlsx/.xlsm files (first sheet each) into one workbook."""
    files = request.files.getlist("files")
    if len(files) < 2:
        return jsonify({"error": "Upload at least 2 files to merge."}), 400
    add_source = request.form.get("add_source") == "1"
    tmp_paths: list[str] = []
    filenames: list[str] = []
    try:
        for f in files:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in (".xlsx", ".xlsm"):
                return jsonify({"error": f"'{f.filename}' is not an .xlsx/.xlsm file."}), 400
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                f.save(tmp.name)
                tmp_paths.append(tmp.name)
            filenames.append(f.filename)
        data = merge_xlsx_to_bytes(tmp_paths, filenames, add_source=add_source)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        for p in tmp_paths:
            _cleanup_tmp(p)
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=merged.xlsx"},
    )


@app.route("/excel/join", methods=["POST"])
def excel_join():
    """Enrich target xlsx with columns from source xlsx, matched on a key column (LEFT JOIN)."""
    target_f = request.files.get("target")
    source_f = request.files.get("source")
    key_col = (request.form.get("key_col") or "").strip()
    columns_to_copy = request.form.getlist("columns")

    if not target_f or not source_f:
        return jsonify({"error": "Upload both a target and a source file."}), 400
    if not key_col:
        return jsonify({"error": "key_col is required."}), 400
    if not columns_to_copy:
        return jsonify({"error": "Select at least one column to copy."}), 400
    for f in (target_f, source_f):
        if os.path.splitext(f.filename or "")[1].lower() not in (".xlsx", ".xlsm"):
            return jsonify({"error": f"'{f.filename}' is not an .xlsx/.xlsm file."}), 400

    target_tmp = source_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as t:
            target_f.save(t.name)
            target_tmp = t.name
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as t:
            source_f.save(t.name)
            source_tmp = t.name
        data = join_xlsx_to_bytes(target_tmp, source_tmp, key_col, columns_to_copy)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(target_tmp)
        _cleanup_tmp(source_tmp)

    base = os.path.splitext(target_f.filename)[0] or "enriched"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={base}_enriched.xlsx"},
    )


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
