#!/usr/bin/env python3
"""
Batch extraction via Docs Garage HTTP API: one .docx/.pdf per request → one .xlsx per file.

Run on the same host as Flask (e.g. SSH to lab, BASE_URL=http://127.0.0.1:35050).

LLM mode defaults to NDJSON progress from /extract (section-by-section lines on stderr, like the UI).
Use --no-llm-progress-stream for one-shot JSON only.

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
from urllib.parse import quote
from urllib.request import Request, urljoin, urlopen

T = TypeVar("T")

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
    return None


def _parse_ndjson_extract_response(resp) -> dict[str, Any]:
    """Read application/x-ndjson body from /extract; print progress to stderr."""
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
                msg = _format_ndjson_progress_line(payload)
                if msg:
                    print(f"  llm: {msg}", file=sys.stderr, flush=True)
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


def post_ndjson_extract(base_url: str, path: Path, form: dict[str, str], timeout: float) -> dict[str, Any]:
    """POST /extract expecting NDJSON progress + final result (LLM with llm_progress_stream on)."""
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
    req = Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urlopen(req, timeout=timeout) as resp:
            if resp.headers.get("X-Extract-Stream") != "1":
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
            return _parse_ndjson_extract_response(resp)
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
    req = Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = {}
        msg = payload.get("error") if isinstance(payload, dict) else None
        if not msg:
            msg = e.reason or str(e.code)
        raise _runtime_http(msg, e.code) from e


def post_download(base_url: str, rows: list[dict[str, Any]], timeout: float) -> bytes:
    url = urljoin(base_url.rstrip("/") + "/", "download")
    body = json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except HTTPError as e:
        msg = f"download HTTP {e.code}: {e.reason}"
        raise _runtime_http(msg, e.code) from e


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
            print(
                f"  retry {attempt + 2}/{max_attempts} in {delay}s ({op_name}): {e}",
                file=sys.stderr,
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
    mode = args.mode.strip().lower()
    form: dict[str, str] = {"mode": mode}
    if mode == "llm":
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
        # Default: omit llm_progress_stream → server uses "1" (NDJSON progress), same as UI.
        if args.no_llm_progress_stream:
            form["llm_progress_stream"] = "0"
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
    if os.environ.get("DOCS_GARAGE_MODE"):
        out["mode"] = os.environ["DOCS_GARAGE_MODE"].strip().lower()
    return out


def _api_urls(base: str) -> tuple[str, str]:
    b = base.rstrip("/") + "/"
    return urljoin(b, "extract"), urljoin(b, "download")


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
    print("[bulk] proxy environment variables are set (urllib may not use direct localhost):", file=sys.stderr)
    for k, v in found:
        short = (v[:100] + "…") if len(v) > 100 else v
        print(f"[bulk]   {k}={short}", file=sys.stderr)
    no_p = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").strip()
    if no_p:
        print(f"[bulk] NO_PROXY={no_p}", file=sys.stderr)
    else:
        print(
            "[bulk] hint: if connections to 127.0.0.1 fail, try: "
            "export NO_PROXY=127.0.0.1,localhost",
            file=sys.stderr,
        )


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
    extract_u, download_u = _api_urls(args.base_url)
    print("[bulk] ══ folder_batch_extract ══", file=sys.stderr)
    print(f"[bulk] source:      {source}", file=sys.stderr)
    print(f"[bulk] output:      {output_dir}", file=sys.stderr)
    print(f"[bulk] files:       {file_count} document(s)", file=sys.stderr)
    print(f"[bulk] failure log: {log_path}", file=sys.stderr)
    print(f"[bulk] mode:        {form.get('mode', args.mode)}", file=sys.stderr)
    print(f"[bulk] base-url:    {args.base_url}", file=sys.stderr)
    print(f"[bulk] POST         {extract_u}", file=sys.stderr)
    print(f"[bulk] POST         {download_u}", file=sys.stderr)
    print(f"[bulk] HTTP timeout per request: {args.timeout}s", file=sys.stderr)
    print(
        f"[bulk] retries:     {'on' if retry_enabled else 'off'} "
        f"(max {max_att} attempt(s) per extract/download)",
        file=sys.stderr,
    )
    print(
        f"[bulk] skip exists: {'no (--force)' if args.force else 'yes (default — resume friendly)'}",
        file=sys.stderr,
    )
    if (form.get("mode") or "").strip().lower() == "llm":
        print(
            f"[bulk] llm scope:   {args.llm_document_scope} · split={args.llm_section_split}",
            file=sys.stderr,
        )
        print(f"[bulk] llm model:   {args.llm_model}", file=sys.stderr)
        print(f"[bulk] llm base:    {args.llm_base_url}", file=sys.stderr)
        print(
            "[bulk] llm progress stream (NDJSON / section lines on stderr): "
            f"{'off (--no-llm-progress-stream)' if args.no_llm_progress_stream else 'on (default)'}",
            file=sys.stderr,
        )
    _log_proxy_env()
    print("[bulk] ══════════════════════════", file=sys.stderr)


def main() -> int:
    env = parse_env_defaults()
    parser = argparse.ArgumentParser(
        description="Extract each .docx/.pdf in a folder to its own .xlsx via Docs Garage /extract + /download.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Directory containing input documents.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory for generated .xlsx files (created if missing).",
    )
    parser.add_argument(
        "--base-url",
        default=env.get("base_url", "http://127.0.0.1:35050"),
        help="Docs Garage base URL (env: DOCS_GARAGE_URL). Default http://127.0.0.1:35050",
    )
    parser.add_argument(
        "--mode",
        choices=("template", "llm"),
        default=env.get("mode", "template"),
        help="Extraction mode (env: DOCS_GARAGE_MODE). Default template.",
    )
    parser.add_argument("--recursive", action="store_true", help="Include .docx/.pdf in subfolders.")
    parser.add_argument(
        "--disambiguate-ext",
        action="store_true",
        help="Name outputs stem_docx.xlsx / stem_pdf.xlsx to avoid collisions.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=720.0,
        help="HTTP timeout per request in seconds (default 720).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=10,
        help="Max attempts per HTTP call for transient errors (default 10).",
    )
    parser.add_argument(
        "--no-retry",
        action="store_true",
        help="Disable retry/backoff (single attempt per request).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even when the target .xlsx already exists (default: skip existing outputs).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Append one JSON line per failure (default: <output>/batch_extract_failures.log).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first failure.",
    )
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="Exit with code 1 if any failure was logged (see --log-file).",
    )
    parser.add_argument(
        "--skip-empty-rows",
        action="store_true",
        help="Treat zero extracted rows as failure for logging and --strict-exit.",
    )

    parser.add_argument("--llm-base-url", default="", help="OpenAI-compatible base URL.")
    parser.add_argument("--llm-api-key", default="", help='API key (use "ollama" for Ollama).')
    parser.add_argument("--llm-model", default="", help="Model id.")
    parser.add_argument(
        "--llm-document-scope",
        choices=("whole", "sections"),
        default="sections",
    )
    parser.add_argument(
        "--llm-heading-level",
        default="auto",
        help="Heading depth for section split: auto or 1–6.",
    )
    parser.add_argument(
        "--llm-section-split",
        choices=("headings", "patterns"),
        default="headings",
    )
    parser.add_argument(
        "--llm-section-regex-hints",
        default="",
        help="Regex hints file path or inline string (required for patterns split).",
    )
    parser.add_argument("--llm-user-hints", default="", help="Optional short hints for the model.")
    parser.add_argument(
        "--llm-stream",
        choices=("0", "1"),
        default=None,
        help="Force LLM streaming off (0) or on (1). Default: server env / Auto.",
    )
    parser.add_argument(
        "--no-llm-progress-stream",
        action="store_true",
        help="Disable NDJSON progress from /extract (one JSON response; no section-by-section lines). "
        "Default is progress stream for LLM mode (matches the web UI).",
    )

    args = parser.parse_args()

    hints_raw = args.llm_section_regex_hints.strip()
    if hints_raw:
        p = Path(hints_raw)
        if p.is_file():
            args.llm_section_regex_hints = p.read_text(encoding="utf-8", errors="replace")
        else:
            args.llm_section_regex_hints = hints_raw

    if args.mode == "llm":
        if not args.llm_base_url or not args.llm_model:
            print(
                "LLM mode requires --llm-base-url and --llm-model "
                "(and usually --llm-api-key).",
                file=sys.stderr,
            )
            return 2
        if args.llm_document_scope == "sections" and args.llm_section_split == "patterns":
            if not args.llm_section_regex_hints.strip():
                print(
                    "patterns split requires non-empty --llm-section-regex-hints.",
                    file=sys.stderr,
                )
                return 2

    source = args.source.resolve()
    output_dir = args.output.resolve()
    if not source.is_dir():
        print(f"Not a directory: {source}", file=sys.stderr)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)

    log_path = args.log_file
    if log_path is None:
        log_path = output_dir / "batch_extract_failures.log"

    form = build_extract_form(args)
    llm_progress_stream = args.mode == "llm" and not args.no_llm_progress_stream
    files = discover_inputs(source, args.recursive)
    if not files:
        print(f"No .docx or .pdf files in {source}", file=sys.stderr)
        return 1

    retry_enabled = not args.no_retry
    max_att = max(1, args.max_retries)

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

    for idx, doc_path in enumerate(files, start=1):
        rel = doc_path.name
        print(f"[{idx}/{len(files)}] {doc_path}", flush=True)
        out_xlsx = output_path_for(doc_path, output_dir, args.disambiguate_ext)
        if not args.force and out_xlsx.is_file():
            skip_count += 1
            print(f"  skip: output exists → {out_xlsx.name}", flush=True)
            continue

        try:

            def do_extract() -> dict[str, Any]:
                return post_extract_dispatch(
                    args.base_url,
                    doc_path,
                    form,
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
                print(f"  FAILED: {fr.get('reason')} {fr.get('detail', '')}", file=sys.stderr)
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if args.fail_fast:
                    return 1
                continue

            if errs:
                for e in errs:
                    print(f"  warning: {e}", file=sys.stderr)

            if args.skip_empty_rows and len(rows) == 0:
                rec = {"file": rel, "phase": "rows", "error": "zero rows"}
                failures.append(rec)
                print("  WARNING: zero rows", file=sys.stderr)
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if args.fail_fast:
                    return 1
                continue

            if len(rows) == 0:
                print("  skip: zero rows (no .xlsx written)")
                continue

            def do_download() -> bytes:
                return post_download(args.base_url, rows, args.timeout)

            xlsx_bytes = call_with_retry(
                "download",
                do_download,
                max_attempts=max_att,
                enabled=retry_enabled,
            )

            out_xlsx.write_bytes(xlsx_bytes)
            ok_count += 1
            print(f"  -> {out_xlsx}")

        except (URLError, RuntimeError, json.JSONDecodeError) as e:
            rec = {"file": rel, "phase": "extract", "error": str(e)}
            failures.append(rec)
            print(f"  ERROR extract: {e}", file=sys.stderr)
            with log_path.open("a", encoding="utf-8") as lf:
                lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if args.fail_fast:
                return 1
        except Exception as e:
            rec = {"file": rel, "phase": "unexpected", "error": repr(e)}
            failures.append(rec)
            print(f"  ERROR unexpected: {e}", file=sys.stderr)
            with log_path.open("a", encoding="utf-8") as lf:
                lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if args.fail_fast:
                return 1

    parts = [
        f"wrote {ok_count} workbook(s)",
        f"skipped {skip_count} existing",
        f"failures logged {len(failures)}",
    ]
    print(f"Done. {', '.join(parts)}.")
    if failures and args.strict_exit:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
