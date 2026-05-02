#!/usr/bin/env python3
"""
Batch extraction via Docs Garage HTTP API: one .docx/.pdf per request → one .xlsx per file.

Run on the same host as Flask (e.g. SSH to lab, BASE_URL=http://127.0.0.1:5000).

Requires the Flask app to be running. Uses stdlib only (no requests dependency).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urljoin, urlopen


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == ".pdf":
        return "application/pdf"
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


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
        parts.append(f'Content-Disposition: form-data; name="{key}"'.encode())
        parts.append(crlf)
        parts.append(value.encode("utf-8"))
        parts.append(crlf)

    parts.append(f"--{boundary}".encode())
    disp = (
        f'Content-Disposition: form-data; name="{file_field_name}"; filename="{filename}"'
    )
    parts.append(disp.encode())
    parts.append(crlf)
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(crlf)
    parts.append(file_bytes)
    parts.append(crlf)
    parts.append(f"--{boundary}--".encode())
    parts.append(crlf)

    body = b"".join(parts)
    ctype = f"multipart/form-data; boundary={boundary}"
    return ctype, body


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
            payload = {"error": e.reason or str(e.code)}
        raise RuntimeError(payload.get("error", str(payload))) from e


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
        raise RuntimeError(f"download HTTP {e.code}: {e.reason}") from e


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
        # Single JSON response (required for this script)
        form["llm_progress_stream"] = "0"
    return form


def parse_env_defaults() -> dict[str, str]:
    """Optional env fallbacks (see scripts/BULK_EXTRACT.md)."""
    out: dict[str, str] = {}
    if os.environ.get("DOCS_GARAGE_URL"):
        out["base_url"] = os.environ["DOCS_GARAGE_URL"].strip()
    if os.environ.get("DOCS_GARAGE_MODE"):
        out["mode"] = os.environ["DOCS_GARAGE_MODE"].strip().lower()
    return out


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
        default=env.get("base_url", "http://127.0.0.1:5000"),
        help="Docs Garage base URL (env: DOCS_GARAGE_URL). Default http://127.0.0.1:5000",
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

    # LLM options (ignored when mode=template)
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
        help='Heading depth for section split: auto or 1–6.',
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

    args = parser.parse_args()

    # Load regex hints from file if path exists
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
    files = discover_inputs(source, args.recursive)
    if not files:
        print(f"No .docx or .pdf files in {source}", file=sys.stderr)
        return 1

    failures: list[dict[str, Any]] = []
    ok_count = 0

    for idx, doc_path in enumerate(files, start=1):
        rel = doc_path.name
        print(f"[{idx}/{len(files)}] {doc_path}", flush=True)
        try:
            data = post_json_extract(args.base_url, doc_path, form, args.timeout)
        except (URLError, RuntimeError, json.JSONDecodeError) as e:
            rec = {"file": rel, "phase": "extract", "error": str(e)}
            failures.append(rec)
            print(f"  ERROR extract: {e}", file=sys.stderr)
            with log_path.open("a", encoding="utf-8") as lf:
                lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if args.fail_fast:
                return 1
            continue

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

        out_xlsx = output_path_for(doc_path, output_dir, args.disambiguate_ext)
        try:
            xlsx_bytes = post_download(args.base_url, rows, args.timeout)
        except (URLError, RuntimeError) as e:
            rec = {"file": rel, "phase": "download", "error": str(e)}
            failures.append(rec)
            print(f"  ERROR download: {e}", file=sys.stderr)
            with log_path.open("a", encoding="utf-8") as lf:
                lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if args.fail_fast:
                return 1
            continue

        out_xlsx.write_bytes(xlsx_bytes)
        ok_count += 1
        print(f"  -> {out_xlsx}")

    print(f"Done. Wrote {ok_count} workbook(s). Failures: {len(failures)}.")
    if failures and args.strict_exit:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
