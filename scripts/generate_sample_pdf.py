#!/usr/bin/env python3
"""Regenerate samples/sample_test_plan.pdf (optional dev helper).

Requires: pip install reportlab
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "samples" / "sample_test_plan.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph("## Verify Sample TC_PDF_SAMP_001", styles["Normal"]))
    story.append(Spacer(1, 14))
    story.append(Paragraph("PDF sample description for Docs Garage.", styles["Normal"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Preconditions", styles["Normal"]))
    story.append(Paragraph("Application is installed and reachable.", styles["Normal"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Steps", styles["Normal"]))
    data = [
        ["#", "Test Step", "Expected Result"],
        ["1", "Open login page", "Login form is shown"],
        ["2", "Submit valid credentials", "User lands on home"],
    ]
    t = Table(data, colWidths=[0.5 * inch, 2.4 * inch, 2.3 * inch])
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ]
        )
    )
    story.append(t)
    doc.build(story)
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
