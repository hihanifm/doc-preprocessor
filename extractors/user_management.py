"""
Extractor for test plans where test cases are identified by headings with
underscore-based ID suffixes (e.g. "Verify Login TC_001_LOGIN").

Procedure content from pipe tables is not split into separate steps vs expected
columns (layouts vary too much). Tables that look like procedure/step/expected
grids—or any table under the test-case body after "Steps"—are flattened to plain
lines and stored in one field: steps_expected.

Generated from: samples/sample_test_plan.docx
"""

import re
from .base import BaseExtractor

_TC_ID = re.compile(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,})\s*$')

_PRECOND_LABELS = {'preconditions', 'precondition', 'applicability', 'prerequisites'}
_STEP_LABELS = {'steps', 'test steps', 'steps and expected results', 'expected results',
                'procedure', 'test procedure', 'test steps:', 'expected results:',
                'steps and expected results:', 'procedure:'}

# Header cells suggesting a procedure / steps / outcomes table (avoid splitting columns)
_PROCEDURE_HDR_HINTS = frozenset({
    'step', 'steps', 'action', 'actions', 'test step', 'test steps',
    'expected', 'expect', 'result', 'results', 'outcome', 'outcomes',
    'procedure', 'procedure step', 'actual', 'pass', 'fail', 'criteria',
    'description', 'verification', 'input', 'output',
})


def _split_row(row: str) -> list[str]:
    return [c.strip() for c in row.split('|') if c.strip()]


def _is_table_row(line: str) -> bool:
    return '|' in line and len(line.strip()) > 2


def _headers_suggest_procedure(first_row: str) -> bool:
    headers = _split_row(first_row)
    if not headers:
        return False
    for h in headers:
        hnorm = h.lower().strip().rstrip(':').strip()
        for hint in _PROCEDURE_HDR_HINTS:
            if hint in hnorm:
                return True
    return False


def _flatten_table(rows: list[str]) -> str:
    """Keep each pipe-row as one line so column structure stays visible in one cell."""
    return '\n'.join(r.strip() for r in rows if r.strip())


def _parse_section(content: str) -> dict:
    lines = content.strip().splitlines()
    description_lines: list[str] = []
    precond_lines: list[str] = []
    tables: list[tuple[str | None, list[str]]] = []
    current_table: list[str] = []
    table_origin: str | None = None

    state = 'description'

    def flush_table():
        nonlocal table_origin
        if current_table:
            tables.append((table_origin, list(current_table)))
            current_table.clear()
            table_origin = None

    for line in lines:
        s = line.strip()
        if not s:
            flush_table()
            continue

        label = s.lower().rstrip(':')
        if label in _PRECOND_LABELS:
            flush_table()
            state = 'preconditions'
            continue
        if label in _STEP_LABELS:
            flush_table()
            state = 'body'
            continue

        if _is_table_row(s):
            if not current_table:
                table_origin = state
            current_table.append(s)
            state = 'body'
        else:
            flush_table()
            if state == 'description':
                description_lines.append(s)
            elif state == 'preconditions':
                precond_lines.append(s)

    flush_table()

    procedure_chunks: list[str] = []
    for origin, rows in tables:
        if not rows:
            continue
        if origin == 'body' or _headers_suggest_procedure(rows[0]):
            procedure_chunks.append(_flatten_table(rows))

    steps_expected = '\n\n'.join(procedure_chunks).strip()

    return {
        'description': '\n'.join(description_lines).strip(),
        'preconditions': '\n'.join(precond_lines).strip(),
        'steps_expected': steps_expected,
    }


class UserManagementExtractor(BaseExtractor):
    name = "Underscore-ID Test Plan"

    def matches(self, doc_text: str) -> bool:
        """Matches documents with ## headings containing underscore-based TC IDs."""
        for line in doc_text.splitlines():
            if line.startswith('## ') and _TC_ID.search(line):
                return True
        return False

    def extract(self, doc_text: str, filename: str) -> list[dict]:
        parts = re.split(r'^(## .+)$', doc_text, flags=re.MULTILINE)
        rows = []

        i = 1
        while i < len(parts) - 1:
            heading_line = parts[i].strip()
            content = parts[i + 1] if i + 1 < len(parts) else ''
            i += 2

            heading_text = heading_line.lstrip('#').strip()
            m = _TC_ID.search(heading_text)
            if not m:
                continue

            test_id = m.group(1)
            test_name = heading_text[: m.start()].strip()
            parsed = _parse_section(content)

            rows.append({
                'file_name': filename,
                'test_id': test_id,
                'test_name': test_name,
                **parsed,
            })

        return rows
