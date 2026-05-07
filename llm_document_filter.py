"""
Drop common front-matter sections before LLM extraction.

Works on normalized reader output where Word heading styles become markdown lines
(# … ######). PDFs often lack those lines — then nothing is stripped.

Sections whose heading titles match (case-insensitive) are removed entirely,
including their bodies, until the next heading at the same or higher outline level.
"""

from __future__ import annotations

import re
from typing import Any

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*)$")


def _normalize_heading_title(title: str) -> str:
    t = title.strip()
    t = re.sub(r"^(?:appendix\s+[a-z0-9]+\s*[.:]\s*)", "", t, flags=re.I)
    t = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", t)
    t = re.sub(r"^(?:chapter|section|part)\s+\d+[.:]\s*", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip().lower()


def _is_boilerplate_heading(norm: str) -> bool:
    """norm is lowercased single-space title without leading numbering."""
    if not norm:
        return False

    exact = frozenset(
        {
            "table of contents",
            "contents",
            "toc",
            "introduction",
            "intro",
            "revision history",
            "revisions",
            "document revision history",
            "change history",
            "version history",
            "document history",
            "record of changes",
            "list of figures",
            "list of tables",
        }
    )
    if norm in exact:
        return True

    if norm == "toc" or norm.startswith("toc "):
        return True

    if re.search(r"\btable of contents\b", norm):
        return True
    if re.search(r"\b(revision|change|version)\s+history\b", norm):
        return True

    # Appendix blocks (same treatment as introduction): heading-only titles and "Appendix A" style.
    if norm in ("appendix", "appendices"):
        return True
    if re.fullmatch(r"appendix(\s+[a-z0-9]{1,8})?", norm):
        return True
    if re.match(r"^appendix(\s+[a-z0-9]{1,8})?\s*:", norm):
        return True

    return False


def _raw_heading_is_appendix_section(title: str) -> bool:
    """
    Appendix titles whose inner topic is stripped by _normalize_heading_title (e.g. 'Appendix A: Glossary')
    must be detected on the raw heading text; normalized form would be just 'glossary'.
    """
    t = title.strip()
    if re.match(r"(?i)^appendix(\s+[a-z0-9]{1,8})?\s*:", t):
        return True
    return False


_TOC_LINE = re.compile(r'\S.*\.{4,}\s*\d+\s*$')


def _strip_toc_lines(text: str) -> str:
    lines = text.split("\n")
    kept = []
    for line in lines:
        check = line.replace("|", " ") if "|" in line else line
        if not _TOC_LINE.search(check):
            kept.append(line)
    return "\n".join(kept)


_TEST_CASE_BODY_SIGNAL = re.compile(
    r"(?i)\b("
    r"expected\s+(?:results?|behaviors?|outcomes?)|"
    r"procedure|precondition|test\s+cases?|"
    r"verification|actual\s+result|"
    r"test\s+(?:objective|description|steps?)|"
    r"given\s+when\s+then"
    r")\b"
)
_STEP_LINE = re.compile(r"(?m)^\s*(?:\d+|[a-z])[\.)]\s+\S")
_LABEL_LINE = re.compile(r"(?mi)^(expected|steps?|procedure|action|result)s?\s*:")
_TC_ID = re.compile(r"(?i)\bTC[-_]\d")

# VZ test case IDs: VZ_TC_ + middle + _ + trailing digits.
# Biased toward recall (catch likely IDs for Excel review / cleanup); extra false positives are OK.
# Case-insensitive prefix; middle allows alnum, underscore, hyphen, dot; boundaries avoid glued junk.
_VZ_TC_ID_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])VZ_TC_[A-Za-z0-9_.-]+_\d+(?![A-Za-z0-9])"
)


def extract_vz_tc_id(text: str) -> str | None:
    """Return the first VZ_TC_* ID in text, or None (liberal match — prefer not missing real IDs)."""
    m = _VZ_TC_ID_RE.search((text or "").strip())
    return m.group(0) if m else None


def section_body_suggests_test_cases(body: str) -> bool:
    """
    Heuristic: section chunk looks like test-case material (steps, expected outcome, tables).

    Used in LLM section mode to skip preamble / narrative blocks and avoid useless API calls.
    """
    s = body.strip()
    if not s:
        return False

    if _TEST_CASE_BODY_SIGNAL.search(s):
        return True
    if re.search(r"(?i)\bsteps?\s*[:\d]", s):
        return True
    if _STEP_LINE.search(s):
        return True
    if _LABEL_LINE.search(s):
        return True
    if _TC_ID.search(s):
        return True

    pipe_lines = sum(1 for line in s.split("\n") if "|" in line)
    if pipe_lines >= 2 and "\n" in s:
        return True

    return False


def strip_boilerplate_heading_sections(text: str) -> tuple[str, dict[str, Any]]:
    """
    Remove markdown-heading sections that look like TOC / intro / appendix / revision blocks.

    Returns (cleaned_text, detail) where detail includes removed titles and lengths.
    """
    lines = text.split("\n")
    out: list[str] = []
    skip_level: int | None = None
    removed: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        m = _HEADING_LINE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if skip_level is not None:
                if level <= skip_level:
                    skip_level = None
                    continue
                i += 1
                continue

            norm = _normalize_heading_title(title)
            if _is_boilerplate_heading(norm) or _raw_heading_is_appendix_section(title):
                removed.append(title)
                skip_level = level
                i += 1
                continue

            out.append(line)
            i += 1
            continue

        if skip_level is None:
            out.append(line)
        i += 1

    cleaned = "\n".join(out)
    detail: dict[str, Any] = {
        "removed_section_titles": removed,
        "chars_before": len(text),
        "chars_after": len(cleaned),
    }

    if not cleaned.strip() and text.strip():
        return text, {**detail, "fallback_original": True, "removed_section_titles": removed}

    return cleaned, detail


def prepare_text_for_llm(raw: str) -> tuple[str, dict[str, Any]]:
    """
    Strip boilerplate heading sections; attach metadata for logging / UI.

    Always returns non-empty text when raw was non-empty (falls back to raw if stripping
    would leave nothing).
    """
    cleaned, detail = strip_boilerplate_heading_sections(raw)
    cleaned = _strip_toc_lines(cleaned)
    meta: dict[str, Any] = {
        "llm_prep_chars_before": detail.get("chars_before", len(raw)),
        "llm_prep_chars_after": len(cleaned),
        "llm_prep_removed_headings": detail.get("removed_section_titles") or [],
        "llm_prep_fallback_original": bool(detail.get("fallback_original")),
    }
    if meta["llm_prep_removed_headings"]:
        meta["llm_prep_stripped"] = True
    else:
        meta["llm_prep_stripped"] = False

    return cleaned, meta
