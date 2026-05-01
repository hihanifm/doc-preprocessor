"""
Digital PDF → plain text for extractors.

Uses pdfplumber for page text and table detection. Pipe-delimited table rows
match the spirit of readers.docx_reader._table_to_text (\" | \" join).

Scanned / image-only PDFs typically yield empty text; OCR is out of scope.
"""

from __future__ import annotations

import pdfplumber


def _normalize_cell(cell: object) -> str:
    if cell is None:
        return ""
    return str(cell).strip().replace("\n", " ")


def _table_rows_to_text(rows: list[list[str | None]] | None) -> str:
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        cells = [_normalize_cell(c) for c in row]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _gap_text(page: pdfplumber.page.Page, bbox: tuple[float, float, float, float]) -> str:
    x0, top, x1, bottom = bbox
    if top >= bottom - 1:
        return ""
    cropped = page.crop((x0, top, x1, bottom))
    txt = cropped.extract_text()
    return txt.strip() if txt else ""


def _page_to_parts(page: pdfplumber.page.Page) -> list[str]:
    """Top-to-bottom interleaving of non-table text and pipe tables."""
    w, h = float(page.width), float(page.height)
    full = (0.0, 0.0, w, h)

    try:
        tables = list(page.find_tables() or [])
    except Exception:
        tables = []

    if not tables:
        txt = page.extract_text()
        if txt and txt.strip():
            return [txt.strip()]
        return []

    tables.sort(key=lambda t: (round(float(t.bbox[1]), 2), round(float(t.bbox[0]), 2)))

    parts: list[str] = []
    y_cursor = 0.0

    for t in tables:
        bbox = t.bbox
        top = float(max(0.0, min(bbox[1], h)))
        bottom = float(max(0.0, min(bbox[3], h)))

        if top > y_cursor + 0.5:
            gap = _gap_text(page, (0.0, y_cursor, w, top))
            if gap:
                parts.append(gap)

        rows = t.extract()
        block = _table_rows_to_text(rows)
        if block:
            parts.append(block)
            parts.append("")

        y_cursor = max(y_cursor, bottom)

    if y_cursor < h - 0.5:
        tail = _gap_text(page, (0.0, y_cursor, w, h))
        if tail:
            parts.append(tail)

    return parts


def read_pdf(path: str) -> str:
    """
    Convert a digital PDF to structured plain text.
    Detected tables become pipe-delimited rows; other text is preserved in
    vertical reading order between table bands (full page width).
    """
    chunks: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts = _page_to_parts(page)
            if parts:
                chunks.append("\n".join(parts))

    return "\n\n".join(chunks).strip()
