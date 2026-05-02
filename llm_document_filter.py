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

    return False


def strip_boilerplate_heading_sections(text: str) -> tuple[str, dict[str, Any]]:
    """
    Remove markdown-heading sections that look like TOC / intro / revision blocks.

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
            if _is_boilerplate_heading(norm):
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
