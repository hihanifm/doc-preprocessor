"""
OpenAI-compatible chat completion → structured test case rows.

Per-request credentials only (caller passes base_url, api_key, model); nothing persisted here.
Uses stdlib urllib only (no extra HTTP dependency).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LIST_MODELS_TIMEOUT_SEC = 30

from exporter import COLUMNS
from llm_document_filter import (
    extract_vz_tc_id,
    prepare_text_for_llm,
    section_body_suggests_test_cases,
)

log = logging.getLogger(__name__)

_LLM_IO_LOG_LOCK = threading.Lock()
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

MAX_DOC_CHARS = 120_000
REQUEST_TIMEOUT_SEC = 180

_ROW_KEYS = [k for k, _ in COLUMNS]

_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.*)$")

_SYSTEM_PROMPT = """You extract software test cases from plain-text documents (Word/PDF-derived).
Respond with ONLY valid JSON — no markdown fences, no commentary.

Exact shape:
{"test_cases":[{"file_name":"","test_id":"","test_name":"","description":"","preconditions":"","procedure_steps":"","expected_results":""}]}

Rules:
- test_cases is an array; use [] if nothing qualifies.
- All values are strings (use "" if unknown).
- If the user message includes "User hints about identifiers", use them to recognize test ids (e.g. tokens like x_y_z) in titles and text.
- If the user message includes "Section: …", only extract test cases from that section's body (it is part of a larger document sent in multiple requests).
- procedure_steps: actions, navigation, inputs, and procedure text only (steps, numbered lists, table "Step" / "Action" columns).
- expected_results: expected outcomes, pass criteria, and table "Expected" / "Expected result" columns — not mixed into procedure_steps when the document separates them.
- If the document only provides a single combined procedure table, put row-aligned step lines in procedure_steps and matching expected lines in expected_results when columns exist; if truly inseparable, put the block in procedure_steps and use "" for expected_results.
- Map headings, numbered sections, and tables into logical test cases when possible.
- test_id: short stable id if the document has one (e.g. TC_001); else synthesize from context or use empty string.
- file_name: echo the filename hint from the user message when unsure.
"""


class LlmExtractError(Exception):
    """User-visible LLM extraction failure (no secrets in message)."""


def _resolve_llm_io_log_path() -> str | None:
    raw = (os.environ.get("LLM_IO_LOG_PATH") or "").strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(_REPO_ROOT, raw))


def _redact_headers_for_log(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() == "authorization" and isinstance(v, str) and v.lower().startswith("bearer "):
            out[k] = "Bearer <redacted>"
        else:
            out[k] = v
    return out


def _append_llm_io_file(text: str) -> None:
    path = _resolve_llm_io_log_path()
    if not path:
        return
    directory = os.path.dirname(path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            log.warning("Could not create directory for LLM_IO_LOG_PATH %r: %s", path, e)
            return
    try:
        with _LLM_IO_LOG_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text)
    except OSError as e:
        log.warning("Could not append LLM_IO_LOG_PATH %r: %s", path, e)


def _llm_io_log_request(
    *,
    url: str,
    headers: dict[str, str],
    model: str,
    file_name: str,
    doc_meta: dict[str, Any],
    use_stream: bool,
    payload: dict[str, Any],
    payload_json: dict[str, Any],
) -> None:
    if not _resolve_llm_io_log_path():
        return
    block = {
        "event": "llm_chat_completion_request",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "model": model,
        "file_name": file_name,
        "doc_meta": doc_meta,
        "stream_enabled": use_stream,
        "headers_redacted": _redact_headers_for_log(headers),
        "payload": payload,
        "payload_with_response_format_json_object": payload_json,
    }
    try:
        body = json.dumps(block, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        log.warning("LLM IO log could not serialize request: %s", e)
        return
    _append_llm_io_file("\n" + "=" * 72 + "\n" + body + "\n")


def _llm_io_log_response_ok(*, assistant_text: str, row_count: int) -> None:
    if not _resolve_llm_io_log_path():
        return
    block = {
        "event": "llm_chat_completion_response",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "assistant_text": assistant_text,
        "normalized_row_count": row_count,
    }
    try:
        body = json.dumps(block, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        log.warning("LLM IO log could not serialize response: %s", e)
        return
    _append_llm_io_file(body + "\n")


def _llm_io_log_response_error(message: str) -> None:
    if not _resolve_llm_io_log_path():
        return
    block = {
        "event": "llm_chat_completion_error",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "error": message,
    }
    try:
        body = json.dumps(block, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        body = json.dumps(block, ensure_ascii=False)
    _append_llm_io_file(body + "\n")


def _truncate(doc_text: str) -> tuple[str, dict[str, Any]]:
    """Return (text for the model, metadata). Metadata is safe to show in the UI."""
    n = len(doc_text)
    if n <= MAX_DOC_CHARS:
        return doc_text, {
            "truncated": False,
            "doc_char_count": n,
            "max_doc_chars": MAX_DOC_CHARS,
        }
    tail = "\n\n[... truncated for model context ...]"
    return doc_text[:MAX_DOC_CHARS] + tail, {
        "truncated": True,
        "doc_char_count": n,
        "max_doc_chars": MAX_DOC_CHARS,
    }


def _strip_json_fence(content: str) -> str:
    c = content.strip()
    if not c.startswith("```"):
        return c
    lines = c.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _empty_vz_tc_placeholder_row(
    file_name: str, section_title: str, test_id: str
) -> dict[str, str]:
    """Excel-only row for empty sections whose title carries a VZ_TC_* id (no LLM call)."""
    title = (section_title or "").strip()
    return {
        "file_name": file_name,
        "test_id": test_id,
        "test_name": title,
        "description": (
            "Empty section (no body text under this heading). Not sent to the LLM — review manually."
        ),
        "preconditions": "",
        "procedure_steps": "",
        "expected_results": "",
    }


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def _normalize_case(raw: dict[str, Any], file_name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in _ROW_KEYS:
        if key == "file_name":
            out[key] = _coerce_str(raw.get("file_name")) or file_name
        else:
            out[key] = _coerce_str(raw.get(key))
    return out


def _chat_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _assistant_content_from_completion(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return (msg.get("content") or "").strip() if isinstance(msg, dict) else ""


def _post_stream_collect(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> str:
    """
    POST chat/completions with stream=true; read OpenAI-style SSE (data: {...}) or a plain JSON body
    if the server ignores stream and returns one shot.
    """
    stream_payload = {**payload, "stream": True}
    body = json.dumps(stream_payload, ensure_ascii=False).encode("utf-8")
    hdrs = {**headers, "Accept": "text/event-stream"}
    req = Request(url, data=body, headers=hdrs, method="POST")
    parts: list[str] = []
    try:
        with urlopen(req, timeout=timeout) as resp:
            ct = (resp.headers.get("Content-Type") or "").lower()
            charset = resp.headers.get_content_charset() or "utf-8"

            # Some endpoints ignore stream:true and return application/json in one chunk.
            if "application/json" in ct and "event-stream" not in ct and "text/event-stream" not in ct:
                raw = resp.read().decode(charset, errors="replace")
                data = json.loads(raw)
                return _assistant_content_from_completion(data)

            while True:
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode(charset, errors="replace").rstrip("\r\n")
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk: dict[str, Any] = json.loads(data_str)
                except json.JSONDecodeError:
                    log.debug("LLM SSE skipped non-JSON line: %r", data_str[:160])
                    continue
                for choice in chunk.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict):
                        c = delta.get("content")
                        if isinstance(c, str) and c:
                            parts.append(c)
                    msg = choice.get("message")
                    if isinstance(msg, dict):
                        c2 = msg.get("content")
                        if isinstance(c2, str) and c2:
                            parts.append(c2)
        return "".join(parts)
    except HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        log.warning(
            "LLM stream POST %s failed HTTP %s (snippet: %r)",
            url,
            e.code,
            (err_body[:300] + "…") if len(err_body) > 300 else err_body,
        )
        hint = f" {err_body}" if err_body else ""
        raise LlmExtractError(f"LLM HTTP error {e.code}.{hint}".strip()) from None
    except TimeoutError as e:
        log.warning("LLM stream POST %s timed out: %s", url, e)
        raise LlmExtractError("LLM request timed out") from None
    except URLError as e:
        log.warning("LLM stream POST %s unreachable: %s", url, e.reason)
        raise LlmExtractError(f"Could not reach LLM server: {e.reason!s}") from None


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        log.warning(
            "LLM POST %s failed HTTP %s (response snippet: %r)",
            url,
            e.code,
            (err_body[:300] + "…") if len(err_body) > 300 else err_body,
        )
        hint = f" {err_body}" if err_body else ""
        raise LlmExtractError(f"LLM HTTP error {e.code}.{hint}".strip()) from None
    except URLError as e:
        log.warning("LLM POST %s unreachable: %s", url, e.reason)
        raise LlmExtractError(f"Could not reach LLM server: {e.reason!s}") from None
    except TimeoutError as e:
        log.warning("LLM POST %s timed out: %s", url, e)
        raise LlmExtractError("LLM request timed out") from None
    except json.JSONDecodeError as e:
        log.warning("LLM POST %s returned invalid JSON: %s", url, e)
        raise LlmExtractError(f"Invalid JSON from LLM server: {e}") from None


def _default_stream_enabled() -> bool:
    v = (os.environ.get("LLM_STREAM") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _default_stream_sections_enabled() -> bool:
    """Default for section-by-section extraction (many sequential requests). Default off."""
    v = (os.environ.get("LLM_STREAM_SECTIONS") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _resolve_stream(document_scope: str, stream: bool | None) -> bool:
    """
    If stream is None, choose by scope: whole file uses LLM_STREAM (default on);
    sections use LLM_STREAM_SECTIONS (default off). Explicit True/False always wins.
    """
    if stream is not None:
        return stream
    ds = (document_scope or "sections").strip().lower()
    if ds == "sections":
        return _default_stream_sections_enabled()
    return _default_stream_enabled()


def _sleep_backoff(attempt: int) -> None:
    # attempt=0 -> 10s, attempt=1 -> 20s (2 exponential backoffs total)
    time.sleep(10 * (2**attempt))


def _truncate_ui_err(msg: str, limit: int = 220) -> str:
    s = (msg or "").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def _is_retryable_llm_server_error(err: Exception) -> bool:
    s = str(err)
    # We only retry "server-ish" failures. This is intentionally conservative: user input
    # problems (400s) should fail fast; server/network flakiness gets two tries.
    return any(
        tok in s
        for tok in (
            "HTTP error 429",
            "HTTP error 500",
            "HTTP error 502",
            "HTTP error 503",
            "HTTP error 504",
            "timed out",
            "Temporary failure",
            "Could not reach LLM server",
        )
    )


def _with_llm_retries(
    fn: Callable[[], Any],
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    file_name: str = "",
    section_title: str | None = None,
) -> Any:
    last: Exception | None = None
    max_attempts = 3  # initial + 2 retries
    for attempt in range(max_attempts):
        try:
            return fn()
        except LlmExtractError as e:
            last = e
            if attempt >= max_attempts - 1 or not _is_retryable_llm_server_error(e):
                raise
            backoff_sec = 10 * (2**attempt)
            log.warning(
                "LLM transient error (attempt %d/%d): %s; backing off %ds",
                attempt + 1,
                max_attempts,
                e,
                backoff_sec,
            )
            if progress:
                title_disp = ""
                if section_title:
                    td = section_title if len(section_title) <= 160 else section_title[:157] + "…"
                    title_disp = td
                progress(
                    {
                        "step": "llm_retry",
                        "file": file_name,
                        "section_title": title_disp or None,
                        "failed_attempt": attempt + 1,
                        "next_attempt": attempt + 2,
                        "max_attempts": max_attempts,
                        "error": _truncate_ui_err(str(e)),
                        "backoff_sec": backoff_sec,
                    }
                )
            _sleep_backoff(attempt)
    assert last is not None
    raise last


def detect_shallowest_heading_level(text: str) -> int | None:
    """Smallest heading depth present (# vs ##). None if no markdown headings."""
    levels: list[int] = []
    for line in text.split("\n"):
        m = _HEADING_LINE_RE.match(line)
        if m:
            levels.append(len(m.group(1)))
    return min(levels) if levels else None


def _compile_section_regex_hints(raw: str) -> list[re.Pattern[str]]:
    """One regex per non-empty line; lines starting with # are ignored (comments)."""
    out: list[re.Pattern[str]] = []
    for line in raw.split("\n"):
        pat = line.strip()
        if not pat or pat.startswith("#"):
            continue
        try:
            out.append(re.compile(pat))
        except re.error as e:
            raise LlmExtractError(f"Invalid section regex {pat!r}: {e}") from None
    return out


def split_document_by_regex_hints(text: str, patterns: list[re.Pattern[str]]) -> list[tuple[str, str]]:
    """
    Start a new section on any line where at least one pattern matches (search on stripped line).
    Section title is the full matching line (truncated). Text before first match is \"(preamble)\".
    """
    lines = text.split("\n")
    sections: list[tuple[str, str]] = []
    buf: list[str] = []
    current_title = ""

    def flush(title: str, body_lines: list[str]) -> None:
        body = "\n".join(body_lines).strip()
        display = title if title else "(preamble)"
        if not body and not title:
            return
        if not body and display == "(preamble)":
            return
        sections.append((display, body))

    def line_opens_section(line: str) -> bool:
        s = line.strip()
        if not s:
            return False
        for pat in patterns:
            if pat.search(s):
                return True
        return False

    for line in lines:
        if line_opens_section(line):
            flush(current_title, buf)
            current_title = line.strip()[:800]
            buf = []
        else:
            buf.append(line)
    flush(current_title, buf)
    return sections


def split_document_by_headings(text: str, target_level: int) -> list[tuple[str, str]]:
    """
    Split on lines that are exactly `target_level` many '#' (Word Heading N → N hashes).
    Returns (section_title, section_body); preamble before first heading is \"(preamble)\".
    """
    lines = text.split("\n")
    sections: list[tuple[str, str]] = []
    buf: list[str] = []
    current_title = ""

    def flush(title: str, body_lines: list[str]) -> None:
        body = "\n".join(body_lines).strip()
        display = title if title else "(preamble)"
        if not body and not title:
            return
        if not body and display == "(preamble)":
            return
        sections.append((display, body))

    for line in lines:
        m = _HEADING_LINE_RE.match(line)
        if m and len(m.group(1)) == target_level:
            flush(current_title, buf)
            current_title = m.group(2).strip()
            buf = []
        else:
            buf.append(line)
    flush(current_title, buf)
    return sections


def _resolve_heading_split_level(doc_text: str, heading_level: str) -> int:
    hl = (heading_level or "auto").strip().lower()
    if hl == "auto":
        found = detect_shallowest_heading_level(doc_text)
        return found if found is not None else 1
    try:
        n = int(hl)
        if 1 <= n <= 6:
            return n
    except ValueError:
        pass
    raise LlmExtractError(f"Invalid heading level {heading_level!r}; use auto or 1–6.")


def _extract_with_llm_single_pass(
    doc_text: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    file_name: str,
    timeout: float = REQUEST_TIMEOUT_SEC,
    stream: bool | None = None,
    section_title: str | None = None,
    user_hints: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """
    One chat/completions call for a single document chunk (whole file or one section).
    """
    url = _chat_url(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    text, doc_meta = _truncate(doc_text)
    sec_line = f"Section: {section_title}\n\n" if section_title else ""
    uh = (user_hints or "").strip()
    hint_line = (
        f"User hints about identifiers / section titles (from human): {uh}\n\n" if uh else ""
    )
    user_content = f"Filename: {file_name}\n{sec_line}{hint_line}--- Document text ---\n\n{text}"

    log.info(
        "LLM extract request chat=%s model=%r file=%r section=%r truncated=%s doc_chars=%s",
        url,
        model,
        file_name,
        section_title,
        doc_meta.get("truncated"),
        doc_meta.get("doc_char_count"),
    )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
    }

    use_stream = _default_stream_enabled() if stream is None else stream

    # Prefer JSON mode when supported (OpenAI); Ollama often rejects — retry without on HTTP 400.
    payload_json = {**payload, "response_format": {"type": "json_object"}}

    _llm_io_log_request(
        url=url,
        headers=headers,
        model=model,
        file_name=file_name,
        doc_meta=doc_meta,
        use_stream=use_stream,
        payload=payload,
        payload_json=payload_json,
    )

    try:
        if progress and section_title is None:
            progress(
                {
                    "step": "whole_llm",
                    "phase": "request",
                    "file": file_name,
                }
            )

        def _do_call() -> str:
            content = ""

            if use_stream:
                log.debug("LLM extract using streaming (SSE)")
                try:
                    content = _post_stream_collect(url, headers, payload_json, timeout)
                except LlmExtractError as first:
                    if "HTTP error 400" not in str(first):
                        raise
                    log.info("LLM stream with json_object rejected HTTP 400; retry stream without response_format")
                    content = _post_stream_collect(url, headers, payload, timeout)

                if not content.strip():
                    log.warning("LLM streaming returned empty assistant text; falling back to non-streaming completion")

            if not content.strip():
                try:
                    data = _post_json(url, headers, payload_json, timeout)
                except LlmExtractError as first:
                    if "HTTP error 400" not in str(first):
                        raise
                    data = _post_json(url, headers, payload, timeout)
                choices = data.get("choices") or []
                if not choices:
                    raise LlmExtractError("LLM returned no choices.")
                try:
                    msg0 = choices[0].get("message")
                    content = (msg0.get("content") or "") if isinstance(msg0, dict) else ""
                except (TypeError, KeyError, IndexError) as e:
                    raise LlmExtractError(f"Unexpected LLM response shape: {e}") from None

            return content

        content = _with_llm_retries(
            _do_call,
            progress=progress,
            file_name=file_name,
            section_title=section_title,
        )

        if not content.strip():
            raise LlmExtractError("LLM returned empty content.")

        try:
            parsed = json.loads(_strip_json_fence(content))
        except json.JSONDecodeError as e:
            raise LlmExtractError(f"Model output was not valid JSON: {e}") from None

        cases = parsed.get("test_cases")
        if cases is None:
            raise LlmExtractError('JSON must contain a "test_cases" array.')
        if not isinstance(cases, list):
            raise LlmExtractError('"test_cases" must be an array.')

        rows: list[dict[str, str]] = []
        for item in cases:
            if not isinstance(item, dict):
                continue
            rows.append(_normalize_case(item, file_name))
        _llm_io_log_response_ok(assistant_text=content, row_count=len(rows))
        if progress and section_title is None:
            progress(
                {
                    "step": "whole_llm",
                    "phase": "done",
                    "file": file_name,
                    "rows_found": len(rows),
                }
            )
        return rows, doc_meta
    except LlmExtractError as e:
        _llm_io_log_response_error(str(e))
        raise


def extract_with_llm_by_sections(
    doc_text: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    file_name: str,
    timeout: float = REQUEST_TIMEOUT_SEC,
    stream: bool | None = None,
    heading_level: str = "auto",
    section_split: str = "headings",
    section_regex_hints: str = "",
    user_hints: str = "",
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """
    Split doc_text on headings or regex line patterns; merge rows from one LLM call per section.

    Empty body + section title contains a VZ_TC_* id (see extract_vz_tc_id) → one placeholder row,
    no LLM call, in document order alongside LLM-extracted rows.
    """
    use_stream = _resolve_stream("sections", stream)
    ss = (section_split or "headings").strip().lower()
    agg_meta: dict[str, Any]
    parts: list[tuple[str, str]]
    target = 0

    if ss == "patterns":
        compiled = _compile_section_regex_hints(section_regex_hints)
        if not compiled:
            raise LlmExtractError(
                "Section split \"regex patterns\" requires at least one non-empty regex line "
                "(e.g. a pattern matching lines that start a test case block)."
            )
        parts = split_document_by_regex_hints(doc_text, compiled)
        matched_any = any(
            p.search(line.strip())
            for line in doc_text.split("\n")
            if line.strip()
            for p in compiled
        )
        if not matched_any:
            log.warning(
                "Section regex patterns matched no line in %r — entire file is one section.",
                file_name,
            )
        log.info(
            "LLM section mode (regex): %d pattern(s) → %d chunk(s) for %r",
            len(compiled),
            len(parts),
            file_name,
        )
        agg_meta = {
            "truncated": False,
            "doc_char_count": len(doc_text),
            "max_doc_chars": MAX_DOC_CHARS,
            "llm_section_mode": True,
            "llm_section_calls": 0,
            "llm_section_split": "patterns",
            "llm_pattern_count": len(compiled),
        }
    else:
        target = _resolve_heading_split_level(doc_text, heading_level)
        parts = split_document_by_headings(doc_text, target)
        log.info(
            "LLM section mode (headings): depth=%d → %d chunk(s) for %r",
            target,
            len(parts),
            file_name,
        )
        agg_meta = {
            "truncated": False,
            "doc_char_count": len(doc_text),
            "max_doc_chars": MAX_DOC_CHARS,
            "llm_section_mode": True,
            "llm_section_calls": 0,
            "llm_section_split": "headings",
            "llm_heading_level_used": target,
        }

    skipped_non_test: list[str] = [
        t
        for t, b in parts
        if b.strip() and not section_body_suggests_test_cases(b)
    ]
    placeholder_titles: list[str] = [
        t for t, b in parts if not b.strip() and extract_vz_tc_id(t)
    ]
    llm_section_count = sum(
        1
        for t, b in parts
        if b.strip() and section_body_suggests_test_cases(b)
    )

    if skipped_non_test:
        log.info(
            "LLM section mode: skipped %d chunk(s) with no test-like signals for %r (titles=%s)",
            len(skipped_non_test),
            file_name,
            [t[:100] + ("…" if len(t) > 100 else "") for t in skipped_non_test[:15]],
        )

    if placeholder_titles:
        log.info(
            "LLM section mode: %d empty-section placeholder row(s) for VZ_TC_* id(s) in %r (titles=%s)",
            len(placeholder_titles),
            file_name,
            [t[:100] + ("…" if len(t) > 100 else "") for t in placeholder_titles[:15]],
        )

    if progress:
        plan: dict[str, Any] = {
            "step": "sections_plan",
            "file": file_name,
            "total_sections": llm_section_count,
            "section_split": ss,
            "sections_skipped_non_test": len(skipped_non_test),
        }
        if placeholder_titles:
            plan["sections_empty_vz_tc_placeholders"] = len(placeholder_titles)
        progress(plan)

    all_rows: list[dict[str, str]] = []
    section_failure_msgs: list[str] = []
    any_trunc = False
    n_calls = 0
    uh = (user_hints or "").strip()
    total_w = llm_section_count

    for title, body in parts:
        if not body.strip():
            vid = extract_vz_tc_id(title)
            if vid:
                all_rows.append(_empty_vz_tc_placeholder_row(file_name, title, vid))
            continue
        if not section_body_suggests_test_cases(body):
            continue
        n_calls += 1
        title_disp = title if len(title) <= 480 else title[:477] + "…"
        if progress:
            progress(
                {
                    "step": "section_start",
                    "file": file_name,
                    "index": n_calls,
                    "total": total_w,
                    "title": title_disp,
                }
            )
        try:
            rows, meta = _extract_with_llm_single_pass(
                body,
                base_url=base_url,
                api_key=api_key,
                model=model,
                file_name=file_name,
                timeout=timeout,
                stream=use_stream,
                section_title=title,
                user_hints=uh or None,
                progress=progress,
            )
        except LlmExtractError as e:
            msg = f'Section "{title_disp}": {e}'
            section_failure_msgs.append(msg)
            log.warning(
                "LLM section extract failed file=%r section=%r: %s",
                file_name,
                title_disp,
                e,
            )
            if progress:
                progress(
                    {
                        "step": "section_failed",
                        "file": file_name,
                        "index": n_calls,
                        "total": total_w,
                        "title": title_disp,
                        "error": str(e),
                    }
                )
            continue
        any_trunc = any_trunc or bool(meta.get("truncated"))
        all_rows.extend(rows)
        if progress:
            progress(
                {
                    "step": "section_done",
                    "file": file_name,
                    "index": n_calls,
                    "rows_in_section": len(rows),
                    "cumulative_rows": len(all_rows),
                }
            )
    agg_meta["truncated"] = any_trunc
    agg_meta["llm_section_calls"] = n_calls
    if section_failure_msgs:
        agg_meta["llm_section_failures"] = section_failure_msgs
    if skipped_non_test:
        agg_meta["llm_section_skipped_non_test"] = skipped_non_test
    if placeholder_titles:
        agg_meta["llm_section_empty_vz_tc_placeholders"] = placeholder_titles
        agg_meta["llm_section_empty_vz_tc_placeholder_count"] = len(placeholder_titles)
    return all_rows, agg_meta


def extract_with_llm(
    doc_text: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    file_name: str,
    timeout: float = REQUEST_TIMEOUT_SEC,
    stream: bool | None = None,
    document_scope: str = "sections",
    heading_level: str = "auto",
    section_split: str = "headings",
    section_regex_hints: str = "",
    user_hints: str = "",
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """
    Call OpenAI-compatible chat/completions. Set document_scope=\"sections\" to split on markdown headings
    or regex line patterns; run one request per section. Optional user_hints text is included in each request.

    Streaming: if stream is None, whole-file extraction follows LLM_STREAM (default on); section mode follows
    LLM_STREAM_SECTIONS (default off). Pass True/False to force.

    Boilerplate sections (TOC, introduction, appendix, revision history, etc.) are dropped when they appear
    as markdown heading lines — see llm_document_filter.prepare_text_for_llm.

    In section mode, a section with an empty body but a VZ_TC_* id in the title yields a placeholder Excel row
    (no LLM call); see extract_vz_tc_id in llm_document_filter.

    Retries: transient server/network failures get 2 exponential backoffs (10s then 20s). When a progress
    callback is provided, step \"llm_retry\" events include the error summary and next attempt countdown for the UI.
    """
    doc_text, prep_meta = prepare_text_for_llm(doc_text)
    if prep_meta.get("llm_prep_stripped"):
        log.info(
            "LLM prep removed boilerplate heading section(s) for %r: %s",
            file_name,
            prep_meta.get("llm_prep_removed_headings"),
        )

    uh = (user_hints or "").strip()
    ds = (document_scope or "sections").strip().lower()
    use_stream = _resolve_stream(document_scope, stream)
    # LLM throttling removed: no server-side rate limiting here.

    if ds == "sections":
        rows, meta = extract_with_llm_by_sections(
            doc_text,
            base_url=base_url,
            api_key=api_key,
            model=model,
            file_name=file_name,
            timeout=timeout,
            stream=use_stream,
            heading_level=heading_level,
            section_split=section_split,
            section_regex_hints=section_regex_hints,
            user_hints=uh,
            progress=progress,
        )
        meta.update(prep_meta)
        return rows, meta
    rows, meta = _extract_with_llm_single_pass(
        doc_text,
        base_url=base_url,
        api_key=api_key,
        model=model,
        file_name=file_name,
        timeout=timeout,
        stream=use_stream,
        section_title=None,
        user_hints=uh or None,
        progress=progress,
    )
    meta.update(prep_meta)
    return rows, meta


def validate_llm_form(base_url: str, api_key: str, model: str) -> str | None:
    """Return error message or None if OK."""
    if not base_url.strip():
        return "LLM base URL is required."
    if not model.strip():
        return "LLM model name is required."
    if not api_key.strip():
        return 'LLM API key is required (use "ollama" for local Ollama).'
    return None


def _get_openai_compatible_model_ids(base_url: str, headers: dict[str, str], timeout: float) -> list[str] | None:
    """GET {base}/models. Returns None if endpoint missing or unusable; raises LlmExtractError on 401/403."""
    url = base_url.rstrip("/") + "/models"
    log.info("Listing models: OpenAI-compatible GET %s (Authorization header: %s)", url, "yes" if headers else "no")
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [str(x["id"]) for x in data.get("data") or [] if x.get("id")]
        log.info("OpenAI-compatible /models returned %d id(s)", len(ids))
        return ids
    except HTTPError as e:
        if e.code in (401, 403):
            log.warning("OpenAI-compatible /models denied HTTP %s for %s", e.code, url)
            raise LlmExtractError(
                f"Listing models was denied (HTTP {e.code}). Check the API key and base URL."
            ) from None
        try:
            body_snip = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            body_snip = ""
        log.warning(
            "OpenAI-compatible /models not used (HTTP %s for %s): %r",
            e.code,
            url,
            body_snip,
        )
        return None
    except TimeoutError:
        log.warning("OpenAI-compatible /models timeout after %ss: %s", timeout, url)
        return None
    except URLError as e:
        log.warning("OpenAI-compatible /models connection error for %s: %s", url, e.reason)
        return None
    except json.JSONDecodeError as e:
        log.warning("OpenAI-compatible /models invalid JSON from %s: %s", url, e)
        return None
    except (TypeError, KeyError) as e:
        log.warning("OpenAI-compatible /models unexpected response shape from %s: %s", url, e)
        return None


def _get_ollama_model_tags(base_url: str, timeout: float) -> list[str] | None:
    """GET {origin}/api/tags — Ollama local registry."""
    u = base_url.strip()
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    parsed = urlparse(u)
    if not parsed.netloc:
        log.warning("Ollama /api/tags: could not parse host from base_url %r", base_url)
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    url = origin + "/api/tags"
    log.info("Listing models: Ollama GET %s (origin from base_url %r)", url, base_url)
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = [str(m["name"]) for m in data.get("models") or [] if m.get("name")]
        log.info("Ollama /api/tags returned %d model name(s)", len(names))
        return names
    except HTTPError as e:
        try:
            body_snip = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            body_snip = ""
        log.warning(
            "Ollama /api/tags HTTP %s for %s (body snippet: %r)",
            e.code,
            url,
            body_snip,
        )
        return None
    except TimeoutError:
        log.warning("Ollama /api/tags timeout after %ss: %s", timeout, url)
        return None
    except URLError as e:
        log.warning("Ollama /api/tags unreachable %s — %s (check host.docker.internal vs 127.0.0.1 if using Docker)", url, e.reason)
        return None
    except json.JSONDecodeError as e:
        log.warning("Ollama /api/tags invalid JSON from %s: %s", url, e)
        return None
    except (TypeError, KeyError) as e:
        log.warning("Ollama /api/tags unexpected response from %s: %s", url, e)
        return None


def fetch_model_ids(base_url: str, api_key: str, timeout: float = LIST_MODELS_TIMEOUT_SEC) -> list[str]:
    """
    Try OpenAI-compatible GET /v1/models (using base_url + /models), then Ollama GET /api/tags on same host.
    api_key may be empty for some local gateways; Ollama /api/tags ignores Bearer.
    """
    if not base_url.strip():
        raise LlmExtractError("Base URL is required to list models.")

    headers: dict[str, str] = {}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    log.info(
        "fetch_model_ids: trying OpenAI path then Ollama; base_url=%r api_key_provided=%s",
        base_url,
        bool(api_key.strip()),
    )

    openai_ids = _get_openai_compatible_model_ids(base_url, headers, timeout)
    if openai_ids:
        out = sorted(set(openai_ids))
        log.info("fetch_model_ids: using OpenAI-compatible list (%d models)", len(out))
        return out

    ollama_ids = _get_ollama_model_tags(base_url, timeout)
    if ollama_ids:
        out = sorted(set(ollama_ids))
        log.info("fetch_model_ids: using Ollama /api/tags list (%d models)", len(out))
        return out

    log.warning(
        "fetch_model_ids: both OpenAI /models and Ollama /api/tags failed or returned empty for base_url=%r",
        base_url,
    )
    raise LlmExtractError(
        "Could not list models. For OpenAI-compatible APIs use a base URL ending in /v1. "
        "For Ollama on the same machine as the app, use http://127.0.0.1:11434/v1. "
        "If this server runs inside Docker and Ollama is on the host, use "
        "http://host.docker.internal:11434/v1 instead (127.0.0.1 inside the container is not the host). "
        "Ensure `ollama serve` is running and reachable from the container."
    )
