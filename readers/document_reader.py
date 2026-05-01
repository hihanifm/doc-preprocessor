"""Dispatch .docx / .pdf to the appropriate reader."""

from __future__ import annotations

import os

from readers.docx_reader import read_docx
from readers.pdf_reader import read_pdf


def read_document(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return read_docx(path)
    if ext == ".pdf":
        return read_pdf(path)
    raise ValueError(f"Unsupported document type {ext!r}. Use .docx or .pdf.")
