"""
Extractor for test plans where test cases are identified by headings with
underscore-based ID suffixes (e.g. "Verify Login TC_001_LOGIN").

Handles two table layouts found in this format:
  - Combined: single table with step + expected columns
  - Split: two tables (steps table then expected results table)

Generated from: samples/sample_test_plan.docx
"""

import re
from .base import BaseExtractor

_TC_ID = re.compile(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,})\s*$')
_HEADING = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

_PRECOND_LABELS = {'preconditions', 'precondition', 'applicability', 'prerequisites'}
_STEP_LABELS = {'steps', 'test steps', 'steps and expected results', 'expected results',
                'test steps:', 'expected results:', 'steps and expected results:'}
_STEP_KW = {'step', 'action', 'test step'}
_RESULT_KW = {'expected', 'result', 'outcome', 'expected result', 'expected outcome'}


def _split_row(row: str) -> list[str]:
    return [c.strip() for c in row.split('|') if c.strip()]


def _is_table_row(line: str) -> bool:
    return '|' in line and len(line.strip()) > 2


def _find_col(headers: list[str], keywords: set[str]) -> int:
    for i, h in enumerate(headers):
        if any(kw in h.lower() for kw in keywords):
            return i
    return -1


def _parse_table(rows: list[str]) -> tuple[list[str], list[str]]:
    """Parse a table into (steps, expected_results). Returns empty lists if unclear."""
    if not rows:
        return [], []
    headers = _split_row(rows[0])
    data = [_split_row(r) for r in rows[1:] if _split_row(r)]

    step_col = _find_col(headers, _STEP_KW)
    result_col = _find_col(headers, _RESULT_KW)

    steps, results = [], []
    for r in data:
        if step_col >= 0 and step_col < len(r):
            steps.append(r[step_col])
        if result_col >= 0 and result_col < len(r):
            results.append(r[result_col])
    return steps, results


_INDEX_HEADERS = {'#', 'no', 'no.', 'num', 'step #', 'step#', 'step no', 'id', 'seq', ''}


def _extract_data_col(rows: list[str]) -> list[str]:
    """Return content values from a steps-only or results-only table.

    2-column tables: col 0 is always the index, col 1 is content.
    3+ column tables: find first non-index column by header name.
    """
    if not rows:
        return []
    headers = _split_row(rows[0])
    if len(headers) <= 2:
        col = min(1, len(headers) - 1)
    else:
        col = next(
            (i for i, h in enumerate(headers)
             if h.lower().strip().rstrip('.') not in _INDEX_HEADERS
             and not re.match(r'^#+$', h.strip())),
            0,
        )

    out = []
    for r in rows[1:]:
        cols = _split_row(r)
        if col < len(cols) and cols[col]:
            out.append(cols[col])
    return out


def _parse_section(content: str) -> dict:
    lines = content.strip().splitlines()
    description_lines: list[str] = []
    precond_lines: list[str] = []
    tables: list[list[str]] = []
    current_table: list[str] = []

    state = 'description'  # description → preconditions → body

    def flush_table():
        if current_table:
            tables.append(list(current_table))
            current_table.clear()

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
            current_table.append(s)  # accumulate rows — flush only on break
            state = 'body'
        else:
            flush_table()  # non-table line breaks any running table
            if state == 'description':
                description_lines.append(s)
            elif state == 'preconditions':
                precond_lines.append(s)

    flush_table()

    # Parse tables into steps + expected results
    steps_out: list[str] = []
    results_out: list[str] = []

    if len(tables) == 1:
        steps_out, results_out = _parse_table(tables[0])
    elif len(tables) >= 2:
        # First table = steps, second = expected results
        steps_out = _extract_data_col(tables[0])
        results_out = _extract_data_col(tables[1])

    return {
        'description': '\n'.join(description_lines).strip(),
        'preconditions': '\n'.join(precond_lines).strip(),
        'steps': '\n'.join(steps_out),
        'expected_results': '\n'.join(results_out),
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
        # Split on ## headings; odd indices = heading text, even = content
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
