"""
OpenAI-compatible chat completion → structured test case rows.

Per-request credentials only (caller passes base_url, api_key, model); nothing persisted here.
Uses stdlib urllib only (no extra HTTP dependency).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LIST_MODELS_TIMEOUT_SEC = 30

from exporter import COLUMNS

MAX_DOC_CHARS = 120_000
REQUEST_TIMEOUT_SEC = 180

_ROW_KEYS = [k for k, _ in COLUMNS]

_SYSTEM_PROMPT = """You extract software test cases from plain-text documents (Word/PDF-derived).
Respond with ONLY valid JSON — no markdown fences, no commentary.

Exact shape:
{"test_cases":[{"file_name":"","test_id":"","test_name":"","description":"","preconditions":"","steps_expected":""}]}

Rules:
- test_cases is an array; use [] if nothing qualifies.
- All values are strings (use "" if unknown).
- steps_expected: put procedure steps, actions, AND expected results / outcomes together in one field (plain text or multi-line). Do not split into separate columns.
- Map headings, numbered sections, and tables into logical test cases when possible.
- test_id: short stable id if the document has one (e.g. TC_001); else synthesize from context or use empty string.
- file_name: echo the filename hint from the user message when unsure.
"""


class LlmExtractError(Exception):
    """User-visible LLM extraction failure (no secrets in message)."""


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


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def _normalize_case(raw: dict[str, Any], file_name: str) -> dict[str, str]:
    steps_expected = _coerce_str(raw.get("steps_expected"))
    if not steps_expected:
        s = _coerce_str(raw.get("steps"))
        e = _coerce_str(raw.get("expected_results"))
        parts = [x for x in (s, e) if x]
        if parts:
            steps_expected = "\n\n".join(parts)

    out: dict[str, str] = {}
    for key in _ROW_KEYS:
        if key == "file_name":
            out[key] = _coerce_str(raw.get("file_name")) or file_name
        elif key == "steps_expected":
            out[key] = steps_expected
        else:
            out[key] = _coerce_str(raw.get(key))
    return out


def _chat_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


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
        hint = f" {err_body}" if err_body else ""
        raise LlmExtractError(f"LLM HTTP error {e.code}.{hint}".strip()) from None
    except URLError as e:
        raise LlmExtractError(f"Could not reach LLM server: {e.reason!s}") from None
    except json.JSONDecodeError as e:
        raise LlmExtractError(f"Invalid JSON from LLM server: {e}") from None


def extract_with_llm(
    doc_text: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    file_name: str,
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """
    Call an OpenAI-compatible POST /v1/chat/completions and normalize rows.
    api_key: use literal \"ollama\" for local Ollama.

    Returns (rows, doc_meta) where doc_meta describes input truncation (truncated, doc_char_count, max_doc_chars).
    """
    url = _chat_url(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    text, doc_meta = _truncate(doc_text)
    user_content = f"Filename: {file_name}\n\n--- Document text ---\n\n{text}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
    }

    # Prefer JSON mode when supported (OpenAI); Ollama often rejects — retry without on HTTP 400.
    payload_json = {**payload, "response_format": {"type": "json_object"}}
    try:
        data = _post_json(url, headers, payload_json, timeout)
    except LlmExtractError as first:
        if "HTTP error 400" not in str(first):
            raise
        data = _post_json(url, headers, payload, timeout)

    try:
        choices = data.get("choices") or []
        if not choices:
            raise LlmExtractError("LLM returned no choices.")
        content = choices[0].get("message", {}).get("content") or ""
        if not content.strip():
            raise LlmExtractError("LLM returned empty content.")
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
    return rows, doc_meta


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
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [str(x["id"]) for x in data.get("data") or [] if x.get("id")]
    except HTTPError as e:
        if e.code in (401, 403):
            raise LlmExtractError(
                f"Listing models was denied (HTTP {e.code}). Check the API key and base URL."
            ) from None
        return None
    except (URLError, TimeoutError, json.JSONDecodeError, TypeError, KeyError):
        return None


def _get_ollama_model_tags(base_url: str, timeout: float) -> list[str] | None:
    """GET {origin}/api/tags — Ollama local registry."""
    u = base_url.strip()
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    parsed = urlparse(u)
    if not parsed.netloc:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    url = origin + "/api/tags"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [str(m["name"]) for m in data.get("models") or [] if m.get("name")]
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, TypeError, KeyError):
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

    openai_ids = _get_openai_compatible_model_ids(base_url, headers, timeout)
    if openai_ids:
        return sorted(set(openai_ids))

    ollama_ids = _get_ollama_model_tags(base_url, timeout)
    if ollama_ids:
        return sorted(set(ollama_ids))

    raise LlmExtractError(
        "Could not list models. For OpenAI-compatible APIs use a base URL ending in /v1. "
        "For Ollama on the same machine as the app, use http://127.0.0.1:11434/v1. "
        "If this server runs inside Docker and Ollama is on the host, use "
        "http://host.docker.internal:11434/v1 instead (127.0.0.1 inside the container is not the host). "
        "Ensure `ollama serve` is running and reachable from the container."
    )
