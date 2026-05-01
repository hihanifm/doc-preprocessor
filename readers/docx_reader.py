from docx import Document
from docx.oxml.ns import qn


def _is_heading(paragraph):
    return paragraph.style.name.startswith("Heading")


def _heading_level(paragraph):
    name = paragraph.style.name  # e.g. "Heading 1"
    parts = name.split()
    try:
        return int(parts[-1])
    except ValueError:
        return 1


def _table_to_text(table):
    lines = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _iter_block_items(doc):
    """Yield paragraphs and tables in document order."""
    from docx.oxml import OxmlElement

    body = doc.element.body
    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "p":
            from docx.text.paragraph import Paragraph
            yield ("paragraph", Paragraph(child, doc))
        elif tag == "tbl":
            from docx.table import Table
            yield ("table", Table(child, doc))


def read_docx(path: str) -> str:
    """
    Convert a .docx file to a structured plain-text representation.
    Headings become '## text', tables become pipe-delimited grids.
    """
    doc = Document(path)
    lines = []

    for kind, item in _iter_block_items(doc):
        if kind == "paragraph":
            text = item.text.strip()
            if not text:
                continue
            if _is_heading(item):
                level = _heading_level(item)
                lines.append(f"{'#' * level} {text}")
            else:
                lines.append(text)
        elif kind == "table":
            lines.append(_table_to_text(item))
            lines.append("")  # blank line after table

    return "\n".join(lines)
