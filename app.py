import os
import subprocess
import tempfile

from flask import Flask, jsonify, render_template, request, Response, send_from_directory

import extractors as extractor_registry
from exporter import to_excel
from readers.docx_reader import read_docx

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
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        doc_text = read_docx(tmp_path)
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
    template_used = None
    tmp_path = None

    for f in files:
        if not f.filename.lower().endswith(".docx"):
            errors.append(f"{f.filename}: only .docx files are supported")
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
                f.save(tmp.name)
                tmp_path = tmp.name

            doc_text = read_docx(tmp_path)
            ext = extractor_registry.find_extractor(doc_text)

            if ext is None:
                errors.append(
                    f"{f.filename}: No template found for this document format. "
                    "Ask your developer to add an extractor in extractors/ "
                    "using the samples/ folder as reference."
                )
                continue

            rows = ext.extract(doc_text, f.filename)
            all_rows.extend(rows)
            template_used = ext.name

        except Exception as e:
            errors.append(f"{f.filename}: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                tmp_path = None

    return jsonify({"rows": all_rows, "errors": errors, "template": template_used})


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
