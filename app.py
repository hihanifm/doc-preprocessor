import os
import subprocess
import tempfile

from flask import Flask, jsonify, render_template, request, Response, send_from_directory

import extractors as extractor_registry
from excel_filter import (
    filter_xlsx_to_bytes,
    peek_distinct,
    sample_sheet_rows,
    workbook_sheet_info,
)
from exporter import to_excel
from readers.document_reader import read_document


def _document_suffix(filename: str | None) -> str | None:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in (".docx", ".pdf") else None

FILTER_MODES = frozenset({"contains", "equals", "not_contains", "starts_with"})

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


@app.route("/health")
def health():
    exts = [e.name for e in extractor_registry.get_extractors()]
    return jsonify({
        "status": "ok",
        "commit": _git_commit(),
        "extractors": exts,
        "extractor_count": len(exts),
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extractors")
def list_extractors():
    return jsonify({"extractors": [e.name for e in extractor_registry.get_extractors()]})


@app.route("/preview-doc", methods=["POST"])
def preview_doc():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    suffix = _document_suffix(f.filename)
    if not suffix:
        return jsonify({"error": "Upload a .docx or .pdf file"}), 400
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        doc_text = read_document(tmp_path)
        return jsonify({"document_text": doc_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


@app.route("/extract", methods=["POST"])
def extract():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    all_rows = []
    errors = []
    file_results: list[dict] = []
    templates_order: list[str] = []
    tmp_path = None

    for f in files:
        display_name = f.filename or "(upload)"
        suffix = _document_suffix(f.filename)
        if not suffix:
            msg = f"{display_name}: only .docx and .pdf files are supported"
            errors.append(msg)
            file_results.append(
                {
                    "filename": display_name,
                    "template": None,
                    "rows": 0,
                    "ok": False,
                    "reason": "unsupported",
                }
            )
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                f.save(tmp.name)
                tmp_path = tmp.name

            doc_text = read_document(tmp_path)
            if not doc_text.strip():
                errors.append(
                    f"{display_name}: No extractable text in this PDF (common for scanned "
                    "documents). OCR is not supported — use a digital/text-based PDF."
                )
                file_results.append(
                    {
                        "filename": display_name,
                        "template": None,
                        "rows": 0,
                        "ok": False,
                        "reason": "empty_text",
                    }
                )
                continue

            ext = extractor_registry.find_extractor(doc_text)

            if ext is None:
                errors.append(
                    f"{display_name}: No extractor template matched this document. "
                    "Ask your developer to add an extractor under extractors/ "
                    "(see samples/ for reference formats)."
                )
                file_results.append(
                    {
                        "filename": display_name,
                        "template": None,
                        "rows": 0,
                        "ok": False,
                        "reason": "no_template",
                    }
                )
                continue

            rows = ext.extract(doc_text, display_name)
            all_rows.extend(rows)
            if ext.name not in templates_order:
                templates_order.append(ext.name)
            file_results.append(
                {
                    "filename": display_name,
                    "template": ext.name,
                    "rows": len(rows),
                    "ok": True,
                }
            )

        except Exception as e:
            errors.append(f"{display_name}: {e}")
            file_results.append(
                {
                    "filename": display_name,
                    "template": None,
                    "rows": 0,
                    "ok": False,
                    "reason": "exception",
                    "detail": str(e),
                }
            )
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                tmp_path = None

    template_used = " · ".join(templates_order) if templates_order else None

    return jsonify(
        {
            "rows": all_rows,
            "errors": errors,
            "template": template_used,
            "file_results": file_results,
        }
    )


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json()
    if not data or "rows" not in data:
        return jsonify({"error": "No data provided"}), 400

    xlsx_bytes = to_excel(data["rows"])
    return Response(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=test_cases.xlsx"},
    )


def _cleanup_tmp(path) -> None:
    if path:
        try:
            os.unlink(path)
        except Exception:
            pass


@app.route("/excel/sheet-info", methods=["POST"])
def excel_sheet_info():
    """Return column headers and row count without listing cell values (large-sheet safe)."""
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Upload a .xlsx or .xlsm file"}), 400
    tmp_path = None
    try:
        sheet_index = int(request.form.get("sheet_index", 0))
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        headers, row_count, sheet_names = workbook_sheet_info(tmp_path, sheet_index)
        return jsonify(
            {"columns": headers, "row_count": row_count, "sheet_names": sheet_names}
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(tmp_path)


@app.route("/excel/sample-rows", methods=["POST"])
def excel_sample_rows():
    """First N data rows as JSON for a compact table preview (on demand only)."""
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Upload a .xlsx or .xlsm file"}), 400
    tmp_path = None
    try:
        sheet_index = int(request.form.get("sheet_index", 0))
        limit = min(max(int(request.form.get("limit", 15)), 1), 50)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        columns, rows = sample_sheet_rows(tmp_path, sheet_index, max_rows=limit)
        return jsonify({"columns": columns, "rows": rows})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(tmp_path)


@app.route("/excel/peek-column", methods=["POST"])
def excel_peek_column():
    """Optional: sample distinct values (capped) — only when user asks."""
    f = request.files.get("file")
    column = (request.form.get("column") or "").strip()
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Upload a .xlsx or .xlsm file"}), 400
    if not column:
        return jsonify({"error": "column is required"}), 400
    tmp_path = None
    try:
        sheet_index = int(request.form.get("sheet_index", 0))
        max_values = min(int(request.form.get("max_values", 30)), 200)
        max_scan = min(int(request.form.get("max_scan", 20000)), 500000)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        values, truncated, scanned = peek_distinct(
            tmp_path,
            column,
            sheet_index=sheet_index,
            max_values=max_values,
            max_scan_rows=max_scan,
        )
        return jsonify(
            {"values": values, "truncated": truncated, "scanned_rows": scanned}
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(tmp_path)


@app.route("/excel/download-filtered", methods=["POST"])
def excel_download_filtered():
    """Shrink a spreadsheet by one column filter into a new workbook."""
    f = request.files.get("file")
    column = (request.form.get("column") or "").strip()
    mode = (request.form.get("mode") or "contains").strip().lower()
    needle = request.form.get("value") or ""
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Upload a .xlsx or .xlsm file"}), 400
    if not column:
        return jsonify({"error": "column is required"}), 400
    if mode not in FILTER_MODES:
        return jsonify({"error": f"mode must be one of: {', '.join(sorted(FILTER_MODES))}"}), 400
    tmp_path = None
    try:
        sheet_index = int(request.form.get("sheet_index", 0))
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        xlsx_bytes = filter_xlsx_to_bytes(tmp_path, column, mode, needle, sheet_index)
        base = os.path.splitext(f.filename)[0] or "filtered"
        out_name = f"{base}_filtered.xlsx"
        return Response(
            xlsx_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={out_name}"},
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _cleanup_tmp(tmp_path)


@app.route("/samples/<path:filename>")
def serve_sample(filename):
    return send_from_directory("samples", filename)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
