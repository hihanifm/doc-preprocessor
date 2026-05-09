import json
import logging
import os
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

class _ExtractCancelled(Exception):
    pass

def _app_version() -> str:
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(root, "VERSION")
        return (open(p, "r", encoding="utf-8").read().strip() or "unknown")[:64]
    except Exception:
        return "unknown"


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
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    all_rows: list = []
    errors: list[str] = []
    file_results: list[dict] = []
    templates_order: list[str] = []
    n_ok = sum(1 for w in work_list if w.get("kind") == "ok")
    fi = 0

    for item in work_list:
        if cancelled and cancelled():
            raise _ExtractCancelled("Cancelled")
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
                if cancelled and cancelled():
                    raise _ExtractCancelled("Cancelled")
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

_JOBS: dict[str, dict] = {}

_ACTIVE_BATCH: dict | None = None
_BATCH_LOCK = threading.Lock()


def _job_create() -> str:
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {
        "events": [],
        "done": False,
        "cancelled": False,
        "cond": threading.Condition(),
    }
    return job_id


def _job_append(job_id: str, event: dict, *, done: bool = False) -> None:
    job = _JOBS[job_id]
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with job["cond"]:
        job["events"].append(line)
        if done:
            job["done"] = True
        job["cond"].notify_all()

def _job_cancel(job_id: str, message: str = "Cancelled") -> None:
    job = _JOBS.get(job_id)
    if job is None:
        return
    with job["cond"]:
        if job.get("done"):
            return
        job["cancelled"] = True
        job["events"].append(json.dumps({"type": "error", "message": message}, ensure_ascii=False) + "\n")
        job["done"] = True
        job["cond"].notify_all()


def _job_is_cancelled(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if job is None:
        return False
    return bool(job.get("cancelled"))


def _job_iter(job_id: str):
    job = _JOBS.get(job_id)
    if job is None:
        return
    idx = 0
    while True:
        with job["cond"]:
            job["cond"].wait_for(lambda: idx < len(job["events"]) or job["done"], timeout=30)
            batch = job["events"][idx:]
            done_now = job["done"]
        for line in batch:
            idx += 1
            yield line.encode("utf-8")
        if done_now and idx >= len(_JOBS[job_id]["events"]):
            break


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "version": _app_version(),
        "commit": _git_commit(),
        "template_extractors_enabled": False,
        "support_upload_enabled": True,
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extractors")
def list_extractors():
    return jsonify({"extractors": [], "template_extractors_enabled": False})


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

    mode = (request.form.get("mode") or "llm").strip().lower()
    if mode != "llm":
        return _extract_reject("Template extractors are disabled. Use mode=llm.", req_id=req_id)
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

    work_list = _stage_uploaded_files(files)
    n_staged_ok = sum(1 for w in work_list if w.get("kind") == "ok")
    n_staged_bad = sum(1 for w in work_list if w.get("kind") == "bad")
    _bu = (llm_base_url or "")[:120]
    log.info(
        "POST /extract staged ip=%s mode=llm staged_ok=%d staged_bad=%d model=%r "
        "llm_base_url_prefix=%r scope=%s split=%s",
        _client_ip(),
        n_staged_ok,
        n_staged_bad,
        llm_model,
        _bu,
        llm_document_scope,
        llm_section_split,
    )

    job_id = _job_create()
    output_path = (request.form.get("output_path") or "").strip() or None
    _JOBS[job_id]["output_path"] = output_path
    # Worker runs outside Flask request context — capture IP once here for logs.
    worker_client_ip = _client_ip()

    def _worker():
        try:
            def _progress(ev: dict[str, Any]) -> None:
                if _job_is_cancelled(job_id):
                    raise _ExtractCancelled("Cancelled")
                _job_append(job_id, {"type": "progress", "data": ev})

            payload = _extract_core(
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
                cancelled=lambda: _job_is_cancelled(job_id),
                progress=_progress,
            )
            log.info(
                "POST /extract job=%s done ip=%s mode=%s rows=%d errors=%d file_results=%d",
                job_id, worker_client_ip, mode,
                len(payload.get("rows") or []),
                len(payload.get("errors") or []),
                len(payload.get("file_results") or []),
            )
            _out = _JOBS[job_id].get("output_path")
            if _out and payload.get("rows"):
                try:
                    _tmp = _out + f".{job_id}.tmp"
                    _parent = os.path.dirname(_out)
                    if _parent:
                        os.makedirs(_parent, exist_ok=True)
                    with open(_tmp, "wb") as _f:
                        _f.write(to_excel(payload["rows"]))
                    os.replace(_tmp, _out)
                    log.info("job_id=%s wrote %d rows to %s", job_id, len(payload["rows"]), _out)
                except Exception:
                    log.exception("job_id=%s failed to write xlsx to %s", job_id, _out)
            _job_append(job_id, {"type": "result", **payload}, done=True)
        except _ExtractCancelled:
            log.info("POST /extract job=%s cancelled ip=%s", job_id, worker_client_ip)
            _job_cancel(job_id, "Cancelled")
        except Exception as e:
            log.exception("POST /extract job=%s worker failed ip=%s", job_id, worker_client_ip)
            _job_append(job_id, {"type": "error", "message": str(e)}, done=True)

    threading.Thread(target=_worker, daemon=True).start()
    r = jsonify({"job_id": job_id})
    r.headers["X-Request-ID"] = req_id
    return r


@app.route("/extract/<job_id>/stream", methods=["GET"])
def extract_stream(job_id: str):
    if job_id not in _JOBS:
        return jsonify({"error": "job not found"}), 404
    return Response(
        stream_with_context(_job_iter(job_id)),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Extract-Stream": "1"},
    )

@app.route("/extract/<job_id>/cancel", methods=["POST"])
def extract_cancel(job_id: str):
    if job_id not in _JOBS:
        return jsonify({"error": "job not found"}), 404
    _job_cancel(job_id, "Cancelled")
    return jsonify({"ok": True})


@app.route("/batch/start", methods=["POST"])
def batch_start():
    global _ACTIVE_BATCH
    output_dir = (request.form.get("output_dir") or "").strip()
    reconnect = (request.form.get("reconnect") or "").strip() in ("1", "true", "yes")
    with _BATCH_LOCK:
        if _ACTIVE_BATCH is not None and not reconnect:
            return jsonify({
                "ok": False,
                "message": "A batch is already running.",
                "output_dir": _ACTIVE_BATCH["output_dir"],
                "started": _ACTIVE_BATCH["started"],
            }), 409
        _ACTIVE_BATCH = {
            "output_dir": output_dir,
            "started": datetime.now(timezone.utc).isoformat(),
        }
    return jsonify({"ok": True})


@app.route("/batch/done", methods=["POST"])
def batch_done():
    global _ACTIVE_BATCH
    with _BATCH_LOCK:
        _ACTIVE_BATCH = None
    return jsonify({"ok": True})


@app.route("/batch/cancel", methods=["POST"])
def batch_cancel():
    global _ACTIVE_BATCH
    with _BATCH_LOCK:
        _ACTIVE_BATCH = None
    return jsonify({"ok": True})


@app.route("/batch/status", methods=["GET"])
def batch_status():
    with _BATCH_LOCK:
        if _ACTIVE_BATCH is None:
            return jsonify({"active": False})
        return jsonify({"active": True, **_ACTIVE_BATCH})


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
