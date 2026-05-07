#!/usr/bin/env python3
"""
Batch extraction via Docs Garage HTTP API: one .docx/.pdf per request → one .xlsx per file.

Run on the same host as Flask (e.g. SSH to lab, BASE_URL=http://127.0.0.1:35050).

LLM mode defaults to NDJSON progress from the extraction stream (section-by-section lines on stderr, like the UI).
Use --no-llm-progress-stream to suppress live progress lines.

Requires the Flask app to be running. Uses stdlib only (no requests dependency).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urljoin, urlopen

T = TypeVar("T")

# One id per `main()` run — prefix every line so background/no-GTTY logs stay grep-friendly.
_BULK_PREFIX = "[bulk]"
# Same value sent as HTTP X-Request-ID so server lines use req_id=… matching this run.
_HTTP_SESSION_ID = ""


def _extra_http_headers() -> dict[str, str]:
    if not _HTTP_SESSION_ID:
        return {}
    return {"X-Request-ID": _HTTP_SESSION_ID}


def _bulk_line(msg: str, *, file=sys.stderr, flush: bool = False) -> None:
    print(f"{_BULK_PREFIX} {msg}", file=file, flush=flush)


# Backoff before each retry after a failure (first retry waits 5s, … then cap at 640s).
RETRY_DELAYS_SEC = (5, 10, 20, 40, 80, 160, 320, 640)
RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == ".pdf":
        return "application/pdf"
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _multipart_file_disposition(file_field_name: str, filename: str) -> str:
    """Content-Disposition for the file part (RFC 5987 filename* when needed)."""
    base = f'form-data; name="{file_field_name}"'
    if not filename:
        return f"Content-Disposition: {base}; filename=\"\""
    # Quotes / CR / LF / non-ASCII: use filename* so parsers keep the part.
    if any(c in filename for c in '"\r\n') or any(ord(c) > 127 for c in filename):
        enc = quote(filename, safe="")
        return f"Content-Disposition: {base}; filename*=UTF-8''{enc}"
    return f'Content-Disposition: {base}; filename="{filename}"'


def encode_multipart(
    fields: dict[str, str],
    file_field_name: str,
    filename: str,
    file_bytes: bytes,
    content_type: str,
) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = []

    for key, value in fields.items():
        parts.append(f"--{boundary}".encode())
        parts.append(crlf)
        parts.append(f'Content-Disposition: form-data; name="{key}"'.encode())
        parts.append(crlf)
        parts.append(crlf)
        parts.append(value.encode("utf-8"))
        parts.append(crlf)

    parts.append(f"--{boundary}".encode())
    parts.append(crlf)
    disp = _multipart_file_disposition(file_field_name, filename)
    parts.append(disp.encode())
    parts.append(crlf)
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(crlf)
    parts.append(crlf)
    parts.append(file_bytes)
    parts.append(crlf)
    parts.append(f"--{boundary}--".encode())
    parts.append(crlf)

    body = b"".join(parts)
    # Quoted boundary keeps strict proxies/nginx parsers happy.
    ctype = f'multipart/form-data; boundary="{boundary}"'
    return ctype, body


def _runtime_http(msg: str, code: int | None) -> RuntimeError:
    ex = RuntimeError(msg)
    setattr(ex, "http_status", code)
    return ex


def _format_ndjson_progress_line(data: dict[str, Any]) -> str | None:
    """Human-readable line for NDJSON progress events (similar idea to the web UI)."""
    step = data.get("step") or ""
    file_hint = data.get("file") or ""
    if step == "file_begin":
        return (
            f"file {data.get('index')}/{data.get('total_files')} "
            f"{file_hint[:60]}{'…' if len(str(file_hint)) > 60 else ''}"
        )
    if step == "sections_plan":
        ss = data.get("section_split") or ""
        n = data.get("total_sections")
        return f"{n} section(s) · split={ss}"
    if step == "section_start":
        title = (data.get("title") or "")[:100]
        return f"section {data.get('index')}/{data.get('total')}: {title}"
    if step == "section_done":
        return (
            f"section done +{data.get('rows_in_section', 0)} rows "
            f"(cumulative {data.get('cumulative_rows', 0)})"
        )
    if step == "section_failed":
        title = (data.get("title") or "")[:80]
        err = (data.get("error") or "")[:120]
        return f"section failed (continuing): {title} — {err}"
    if step == "whole_llm":
        if data.get("phase") == "request":
            return "whole-document LLM request…"
        if data.get("phase") == "done":
            return f"whole-document done · {data.get('rows_found', 0)} row(s)"
    if step == "llm_retry":
        na = data.get("next_attempt")
        ma = data.get("max_attempts")
        bs = data.get("backoff_sec")
        err = (data.get("error") or "")[:160]
        return f"retry next {na}/{ma} in {bs}s — {err}"
    return None


def _parse_ndjson_extract_response(resp, *, print_progress: bool = True) -> dict[str, Any]:
    """Read application/x-ndjson body from an extract stream; optionally print progress to stderr."""
    last: dict[str, Any] | None = None
    buf = b""
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            t = line.strip()
            if not t:
                continue
            o = json.loads(t.decode("utf-8", errors="replace"))
            typ = o.get("type")
            if typ == "progress":
                payload = o.get("data") or {}
                msg = _format_ndjson_progress_line(payload) if print_progress else None
                if msg and print_progress:
                    _bulk_line(f"  llm: {msg}", flush=True)
            elif typ == "result":
                last = o
            elif typ == "error":
                raise RuntimeError(o.get("message") or "Extract failed")
            else:
                pass

    tail = buf.strip()
    if tail:
        o = json.loads(tail.decode("utf-8", errors="replace"))
        typ = o.get("type")
        if typ == "result":
            last = o
        elif typ == "error":
            raise RuntimeError(o.get("message") or "Extract failed")

    if not last or last.get("type") != "result":
        raise RuntimeError("Incomplete extraction stream (no final result)")

    return {
        "rows": last.get("rows") or [],
        "errors": last.get("errors") or [],
        "template": last.get("template"),
        "file_results": last.get("file_results") or [],
    }

def _post_extract_job(base_url: str, path: Path, form: dict[str, str], timeout: float) -> str:
    """POST /extract and return {job_id}."""
    rel = "/extract"
    url = urljoin(base_url.rstrip("/") + "/", rel.lstrip("/"))
    file_bytes = path.read_bytes()
    ctype, body = encode_multipart(
        form,
        "files",
        path.name,
        file_bytes,
        _guess_mime(path),
    )
    req = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": ctype, **_extra_http_headers()},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
            job_id = payload.get("job_id") if isinstance(payload, dict) else None
            if not job_id:
                raise RuntimeError("Extract response missing job_id")
            return str(job_id)
    except HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = {}
        msg = payload.get("error") if isinstance(payload, dict) else None
        if not msg:
            msg = e.reason or str(e.code)
        raise _runtime_http(msg, e.code) from e


def post_ndjson_extract(base_url: str, path: Path, form: dict[str, str], timeout: float) -> dict[str, Any]:
    """POST /extract then read NDJSON stream with progress."""
    try:
        job_id = _post_extract_job(base_url, path, form, timeout)
        rel = f"/extract/{job_id}/stream"
        url = urljoin(base_url.rstrip("/") + "/", rel.lstrip("/"))
        req = Request(url, method="GET", headers={**_extra_http_headers()})
        with urlopen(req, timeout=timeout) as resp:
            return _parse_ndjson_extract_response(resp, print_progress=True)
    except HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = {}
        msg = payload.get("error") if isinstance(payload, dict) else None
        if not msg:
            msg = e.reason or str(e.code)
        raise _runtime_http(msg, e.code) from e


def post_json_extract(base_url: str, path: Path, form: dict[str, str], timeout: float) -> dict[str, Any]:
    """POST /extract then read NDJSON stream without printing progress."""
    try:
        job_id = _post_extract_job(base_url, path, form, timeout)
        rel = f"/extract/{job_id}/stream"
        url = urljoin(base_url.rstrip("/") + "/", rel.lstrip("/"))
        req = Request(url, method="GET", headers={**_extra_http_headers()})
        with urlopen(req, timeout=timeout) as resp:
            return _parse_ndjson_extract_response(resp, print_progress=False)
    except HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = {}
        msg = payload.get("error") if isinstance(payload, dict) else None
        if not msg:
            msg = e.reason or str(e.code)
        raise _runtime_http(msg, e.code) from e


def _post_batch_start(base_url: str, output_dir: str, reconnect: bool, timeout: float) -> dict:
    url = urljoin(base_url.rstrip("/") + "/", "batch/start")
    fields: dict[str, str] = {"output_dir": output_dir}
    if reconnect:
        fields["reconnect"] = "1"
    body = urlencode(fields).encode("utf-8")
    req = Request(url, data=body, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded", **_extra_http_headers()})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            raise _runtime_http(f"batch/start HTTP {e.code}", e.code) from e


def _post_batch_done(base_url: str, timeout: float) -> None:
    url = urljoin(base_url.rstrip("/") + "/", "batch/done")
    req = Request(url, data=b"", method="POST", headers={**_extra_http_headers()})
    try:
        with urlopen(req, timeout=min(timeout, 10.0)) as resp:
            resp.read()
    except Exception:
        pass


def _post_batch_cancel(base_url: str, timeout: float) -> None:
    url = urljoin(base_url.rstrip("/") + "/", "batch/cancel")
    req = Request(url, data=b"", method="POST", headers={**_extra_http_headers()})
    try:
        with urlopen(req, timeout=min(timeout, 10.0)) as resp:
            resp.read()
    except Exception:
        pass


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, URLError):
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True
    if isinstance(exc, RuntimeError):
        code = getattr(exc, "http_status", None)
        if code is not None and code in RETRYABLE_HTTP:
            return True
    return False


def call_with_retry(
    op_name: str,
    fn: Callable[[], T],
    *,
    max_attempts: int,
    enabled: bool,
) -> T:
    if not enabled or max_attempts < 2:
        return fn()
    last: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if not is_retryable(e) or attempt >= max_attempts - 1:
                raise
            delay = RETRY_DELAYS_SEC[min(attempt, len(RETRY_DELAYS_SEC) - 1)]
            _bulk_line(
                f"  retry {attempt + 2}/{max_attempts} in {delay}s ({op_name}): {e}",
                flush=True,
            )
            time.sleep(delay)
    assert last is not None
    raise last


def discover_inputs(source: Path, recursive: bool) -> list[Path]:
    exts = {".docx", ".pdf"}
    out: list[Path] = []
    if recursive:
        for p in sorted(source.rglob("*")):
            if p.is_file() and p.suffix.lower() in exts:
                out.append(p)
    else:
        for p in sorted(source.iterdir()):
            if p.is_file() and p.suffix.lower() in exts:
                out.append(p)
    return out


def output_path_for(
    input_path: Path,
    output_dir: Path,
    disambiguate_ext: bool,
) -> Path:
    stem = input_path.stem
    if disambiguate_ext:
        suf = input_path.suffix.lower().lstrip(".") or "bin"
        name = f"{stem}_{suf}.xlsx"
    else:
        name = f"{stem}.xlsx"
    return output_dir / name


def build_extract_form(args: argparse.Namespace) -> dict[str, str]:
    form: dict[str, str] = {"mode": "llm"}
    form["llm_base_url"] = args.llm_base_url
    form["llm_api_key"] = args.llm_api_key
    form["llm_model"] = args.llm_model
    form["llm_document_scope"] = args.llm_document_scope
    form["llm_heading_level"] = args.llm_heading_level
    form["llm_section_split"] = args.llm_section_split
    form["llm_section_regex_hints"] = args.llm_section_regex_hints
    form["llm_user_hints"] = args.llm_user_hints
    if args.llm_stream is not None:
        form["llm_stream"] = args.llm_stream
    return form


def post_extract_dispatch(
    base_url: str,
    path: Path,
    form: dict[str, str],
    timeout: float,
    *,
    llm_progress_stream: bool,
) -> dict[str, Any]:
    mode = (form.get("mode") or "template").strip().lower()
    if mode == "llm" and llm_progress_stream:
        return post_ndjson_extract(base_url, path, form, timeout)
    return post_json_extract(base_url, path, form, timeout)


def parse_env_defaults() -> dict[str, str]:
    out: dict[str, str] = {}
    if os.environ.get("DOCS_GARAGE_URL"):
        out["base_url"] = os.environ["DOCS_GARAGE_URL"].strip()
    return out


_BULK_CONFIG_VERSION = 1
# Same keys as argparse `dest` in main() plus `version` (metadata only).
_BULK_CONFIG_ALLOWED_KEYS = frozenset(
    {
        "version",
        "source",
        "output",
        "base_url",
        "mode",
        "recursive",
        "disambiguate_ext",
        "timeout",
        "max_retries",
        "no_retry",
        "force",
        "log_file",
        "fail_fast",
        "strict_exit",
        "skip_empty_rows",
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_document_scope",
        "llm_heading_level",
        "llm_section_split",
        "llm_section_regex_hints",
        "llm_user_hints",
        "llm_stream",
        "no_llm_progress_stream",
    }
)


def _default_merged_dict(env: dict[str, str]) -> dict[str, Any]:
    return {
        "source": None,
        "output": None,
        "base_url": env.get("base_url", "http://127.0.0.1:35050"),
        "mode": "llm",
        "recursive": False,
        "disambiguate_ext": False,
        "timeout": 720.0,
        "max_retries": 10,
        "no_retry": False,
        "force": False,
        "log_file": None,
        "fail_fast": False,
        "strict_exit": False,
        "skip_empty_rows": False,
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_model": "",
        "llm_document_scope": "sections",
        "llm_heading_level": "auto",
        "llm_section_split": "headings",
        "llm_section_regex_hints": "",
        "llm_user_hints": "",
        "llm_stream": None,
        "no_llm_progress_stream": False,
    }


def _load_bulk_config_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    extra = set(raw) - _BULK_CONFIG_ALLOWED_KEYS
    if extra:
        raise ValueError(f"unknown config keys: {sorted(extra)}")
    if raw.get("version") != _BULK_CONFIG_VERSION:
        raise ValueError(f"config version must be {_BULK_CONFIG_VERSION}")
    return {k: v for k, v in raw.items() if k != "version"}


def _coerce_config_values(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize JSON values to the same types argparse would produce."""
    out = dict(cfg)
    if "source" in out and out["source"] is not None:
        out["source"] = Path(out["source"])
    if "output" in out and out["output"] is not None:
        out["output"] = Path(out["output"])
    if out.get("log_file") is not None:
        out["log_file"] = Path(out["log_file"])
    if "mode" in out and out["mode"] != "llm":
        raise ValueError("mode must be llm")
    if "llm_document_scope" in out and out["llm_document_scope"] not in ("whole", "sections"):
        raise ValueError("llm_document_scope must be whole or sections")
    if "llm_section_split" in out and out["llm_section_split"] not in ("headings", "patterns"):
        raise ValueError("llm_section_split must be headings or patterns")
    if "timeout" in out:
        out["timeout"] = float(out["timeout"])
    if "max_retries" in out:
        out["max_retries"] = int(out["max_retries"])
    for b in (
        "recursive",
        "disambiguate_ext",
        "no_retry",
        "force",
        "fail_fast",
        "strict_exit",
        "skip_empty_rows",
        "no_llm_progress_stream",
    ):
        if b in out and not isinstance(out[b], bool):
            raise ValueError(f"{b} must be a JSON boolean")
    if "llm_stream" in out and out["llm_stream"] is not None:
        s = str(out["llm_stream"])
        if s not in ("0", "1"):
            raise ValueError("llm_stream must be '0' or '1'")
        out["llm_stream"] = s
    hl = out.get("llm_heading_level")
    if hl is not None and hl != "auto" and str(hl) not in ("1", "2", "3", "4", "5", "6"):
        raise ValueError("llm_heading_level must be auto or 1–6")
    if hl is not None:
        out["llm_heading_level"] = str(hl)
    return out


def _log_proxy_env() -> None:
    keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    found = [(k, os.environ[k]) for k in keys if os.environ.get(k)]
    if not found:
        return
    _bulk_line("proxy environment variables are set (urllib may not use direct localhost):")
    for k, v in found:
        short = (v[:100] + "…") if len(v) > 100 else v
        _bulk_line(f"  {k}={short}")
    no_p = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").strip()
    if no_p:
        _bulk_line(f"NO_PROXY={no_p}")
    else:
        _bulk_line("hint: if connections to 127.0.0.1 fail, try: export NO_PROXY=127.0.0.1,localhost")


def _log_startup(
    *,
    args: argparse.Namespace,
    form: dict[str, str],
    source: Path,
    output_dir: Path,
    log_path: Path,
    file_count: int,
    retry_enabled: bool,
    max_att: int,
) -> None:
    extract_u = urljoin(args.base_url.rstrip("/") + "/", "extract")
    _bulk_line("══ folder_batch_extract ══")
    _bulk_line(
        f"session X-Request-ID={_HTTP_SESSION_ID} "
        "(server logs use req_id=… same value when LOG_LEVEL includes INFO)",
        file=sys.stdout,
        flush=True,
    )
    _bulk_line(f"source:      {source}")
    _bulk_line(f"output:      {output_dir}")
    _bulk_line(f"files:       {file_count} document(s)")
    _bulk_line(f"failure log: {log_path}")
    _bulk_line(f"mode:        {form.get('mode', args.mode)}")
    _bulk_line(f"base-url:    {args.base_url}")
    _bulk_line(f"POST         {extract_u}")
    _bulk_line(f"HTTP timeout per request: {args.timeout}s")
    _bulk_line(
        f"retries:     {'on' if retry_enabled else 'off'} "
        f"(max {max_att} attempt(s) per extract/download)"
    )
    _bulk_line(f"skip exists: {'no (--force)' if args.force else 'yes (default — resume friendly)'}")
    if (form.get("mode") or "").strip().lower() == "llm":
        _bulk_line(f"llm scope:   {args.llm_document_scope} · split={args.llm_section_split}")
        _bulk_line(f"llm model:   {args.llm_model}")
        _bulk_line(f"llm base:    {args.llm_base_url}")
        _bulk_line(
            "llm progress stream (NDJSON / section lines on stderr): "
            f"{'off (--no-llm-progress-stream)' if args.no_llm_progress_stream else 'on (default)'}"
        )
    _log_proxy_env()
    _bulk_line("══════════════════════════")


def main() -> int:
    env = parse_env_defaults()
    parser = argparse.ArgumentParser(
        description="Extract each .docx/.pdf in a folder to its own .xlsx via Docs Garage /extract + /download.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="JSON config (version + same keys as CLI). Explicit CLI flags override the file.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=argparse.SUPPRESS,
        help="Directory containing input documents (required unless set in --config).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=argparse.SUPPRESS,
        help="Directory for generated .xlsx files (required unless set in --config).",
    )
    parser.add_argument(
        "--base-url",
        default=argparse.SUPPRESS,
        help="Docs Garage base URL (env: DOCS_GARAGE_URL). Default http://127.0.0.1:35050",
    )
    parser.add_argument(
        "--mode",
        choices=("llm",),
        default=argparse.SUPPRESS,
        help="Extraction mode. Only 'llm' is supported.",
    )
    parser.add_argument("--recursive", action="store_true", default=argparse.SUPPRESS, help="Include .docx/.pdf in subfolders.")
    parser.add_argument(
        "--disambiguate-ext",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Name outputs stem_docx.xlsx / stem_pdf.xlsx to avoid collisions.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=argparse.SUPPRESS,
        help="HTTP timeout per request in seconds (default 720).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=argparse.SUPPRESS,
        help="Max attempts per HTTP call for transient errors (default 10).",
    )
    parser.add_argument(
        "--no-retry",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable retry/backoff (single attempt per request).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Re-extract even when the target .xlsx already exists (default: skip existing outputs).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="Append one JSON line per failure (default: <output>/batch_extract_failures.log).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Stop on first failure.",
    )
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Exit with code 1 if any failure was logged (see --log-file).",
    )
    parser.add_argument(
        "--skip-empty-rows",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Treat zero extracted rows as failure for logging and --strict-exit.",
    )

    parser.add_argument("--llm-base-url", default=argparse.SUPPRESS, help="OpenAI-compatible base URL.")
    parser.add_argument("--llm-api-key", default=argparse.SUPPRESS, help='API key (use "ollama" for Ollama).')
    parser.add_argument("--llm-model", default=argparse.SUPPRESS, help="Model id.")
    parser.add_argument(
        "--llm-document-scope",
        choices=("whole", "sections"),
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--llm-heading-level",
        default=argparse.SUPPRESS,
        help="Heading depth for section split: auto or 1–6.",
    )
    parser.add_argument(
        "--llm-section-split",
        choices=("headings", "patterns"),
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--llm-section-regex-hints",
        default=argparse.SUPPRESS,
        help="Regex hints file path or inline string (required for patterns split).",
    )
    parser.add_argument("--llm-user-hints", default=argparse.SUPPRESS, help="Optional short hints for the model.")
    parser.add_argument(
        "--llm-stream",
        choices=("0", "1"),
        default=argparse.SUPPRESS,
        help="Force LLM streaming off (0) or on (1). Default: server env / Auto.",
    )
    parser.add_argument(
        "--no-llm-progress-stream",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable NDJSON progress from /extract (one JSON response; no section-by-section lines). "
        "Default is progress stream for LLM mode (matches the web UI).",
    )
    parser.add_argument(
        "--cancel",
        action="store_true",
        default=False,
        help="Cancel the currently running batch and exit.",
    )
    parser.add_argument(
        "--reconnect",
        action="store_true",
        default=False,
        help="Take over a running batch and continue from where it left off.",
    )

    parsed = parser.parse_args()

    merged: dict[str, Any] = _default_merged_dict(env)
    if parsed.config is not None:
        try:
            merged.update(_coerce_config_values(_load_bulk_config_file(parsed.config)))
        except ValueError as e:
            print(f"folder_batch_extract: {e}", file=sys.stderr)
            return 2
    for k in vars(parsed):
        if k == "config":
            continue
        merged[k] = getattr(parsed, k)

    if merged.get("source") is None or merged.get("output") is None:
        print(
            "folder_batch_extract: source and output required (JSON config and/or --source/--output)",
            file=sys.stderr,
        )
        return 2

    args = argparse.Namespace(**merged)

    if args.cancel:
        _post_batch_cancel(args.base_url, args.timeout)
        _bulk_line("Batch cancelled.", file=sys.stdout)
        return 0

    global _BULK_PREFIX, _HTTP_SESSION_ID
    _sid = uuid.uuid4().hex[:12]
    _HTTP_SESSION_ID = _sid
    _BULK_PREFIX = f"[bulk run={_sid}]"

    hints_raw = args.llm_section_regex_hints.strip()
    if hints_raw:
        p = Path(hints_raw)
        if p.is_file():
            args.llm_section_regex_hints = p.read_text(encoding="utf-8", errors="replace")
        else:
            args.llm_section_regex_hints = hints_raw

    if args.mode == "llm":
        if not args.llm_base_url or not args.llm_model:
            _bulk_line("LLM mode requires --llm-base-url and --llm-model (and usually --llm-api-key).")
            return 2
        if args.llm_document_scope == "sections" and args.llm_section_split == "patterns":
            if not args.llm_section_regex_hints.strip():
                _bulk_line("patterns split requires non-empty --llm-section-regex-hints.")
                return 2

    source = args.source.resolve()
    output_dir = args.output.resolve()
    if not source.is_dir():
        _bulk_line(f"Not a directory: {source}")
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)

    log_path = args.log_file
    if log_path is None:
        log_path = output_dir / "batch_extract_failures.log"

    form = build_extract_form(args)
    llm_progress_stream = args.mode == "llm" and not args.no_llm_progress_stream
    files = discover_inputs(source, args.recursive)
    if not files:
        _bulk_line(f"No .docx or .pdf files in {source}")
        return 1

    retry_enabled = not args.no_retry
    max_att = max(1, args.max_retries)

    batch_result = _post_batch_start(args.base_url, str(output_dir), args.reconnect, args.timeout)
    if not batch_result.get("ok"):
        _bulk_line(
            f"Batch already running (output: {batch_result.get('output_dir')}, "
            f"started: {batch_result.get('started')}).\n"
            "[bulk] Use --reconnect to take over or --cancel to stop.",
            file=sys.stderr,
        )
        return 1

    _log_startup(
        args=args,
        form=form,
        source=source,
        output_dir=output_dir,
        log_path=log_path,
        file_count=len(files),
        retry_enabled=retry_enabled,
        max_att=max_att,
    )

    failures: list[dict[str, Any]] = []
    ok_count = 0
    skip_count = 0

    try:
        for idx, doc_path in enumerate(files, start=1):
            rel = doc_path.name
            _bulk_line(f"[{idx}/{len(files)}] {doc_path}", file=sys.stdout, flush=True)
            out_xlsx = output_path_for(doc_path, output_dir, args.disambiguate_ext)
            if not args.force and out_xlsx.is_file():
                skip_count += 1
                _bulk_line(f"  skip: output exists → {out_xlsx.name}", file=sys.stdout, flush=True)
                continue

            file_form = {**form, "output_path": str(out_xlsx)}

            try:

                def do_extract() -> dict[str, Any]:
                    return post_extract_dispatch(
                        args.base_url,
                        doc_path,
                        file_form,
                        args.timeout,
                        llm_progress_stream=llm_progress_stream,
                    )

                data = call_with_retry(
                    "extract",
                    do_extract,
                    max_attempts=max_att,
                    enabled=retry_enabled,
                )

                errs = data.get("errors") or []
                rows = data.get("rows") or []
                frs = data.get("file_results") or []

                fr = frs[0] if frs else {}
                if not fr.get("ok", True):
                    rec = {
                        "file": rel,
                        "phase": "extract",
                        "reason": fr.get("reason"),
                        "detail": fr.get("detail"),
                        "errors": errs,
                    }
                    failures.append(rec)
                    _bulk_line(f"  FAILED: {fr.get('reason')} {fr.get('detail', '')}")
                    with log_path.open("a", encoding="utf-8") as lf:
                        lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if args.fail_fast:
                        return 1
                    continue

                if errs:
                    for e in errs:
                        _bulk_line(f"  warning: {e}")

                if args.skip_empty_rows and len(rows) == 0:
                    rec = {"file": rel, "phase": "rows", "error": "zero rows"}
                    failures.append(rec)
                    _bulk_line("  WARNING: zero rows")
                    with log_path.open("a", encoding="utf-8") as lf:
                        lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if args.fail_fast:
                        return 1
                    continue

                if len(rows) == 0:
                    _bulk_line("  skip: zero rows (no .xlsx written)", file=sys.stdout)
                    continue

                if out_xlsx.is_file():
                    ok_count += 1
                    _bulk_line(f"  -> {out_xlsx}", file=sys.stdout)
                else:
                    _bulk_line(f"  WARNING: server did not write {out_xlsx.name}", file=sys.stdout)

            except (URLError, RuntimeError, json.JSONDecodeError) as e:
                rec = {"file": rel, "phase": "extract", "error": str(e)}
                failures.append(rec)
                _bulk_line(f"  ERROR extract: {e}")
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if args.fail_fast:
                    return 1
            except Exception as e:
                rec = {"file": rel, "phase": "unexpected", "error": repr(e)}
                failures.append(rec)
                _bulk_line(f"  ERROR unexpected: {e}")
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if args.fail_fast:
                    return 1

        parts = [
            f"wrote {ok_count} workbook(s)",
            f"skipped {skip_count} existing",
            f"failures logged {len(failures)}",
        ]
        _bulk_line(f"Done. {', '.join(parts)}.", file=sys.stdout)
        if failures and args.strict_exit:
            return 1
        return 0
    finally:
        _post_batch_done(args.base_url, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
