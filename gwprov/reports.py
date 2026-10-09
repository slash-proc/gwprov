"""Portable human-readable rendering for structured GWProv JSON reports."""
from __future__ import annotations

import html
import json
from pathlib import Path


def render_report(source: str | Path, output: str | Path, *, format: str) -> Path:
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    report = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("GWProv report root must be a JSON object")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if format == "html":
        title = str(report.get("title") or source_path.stem)
        body = []
        for key, value in report.items():
            encoded = json.dumps(value, indent=2, ensure_ascii=False, default=str)
            escaped = html.escape(encoded)
            body.append(f"<details open><summary>{html.escape(str(key))}</summary>"
                        f"<pre>{escaped}</pre></details>")
        document = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>""" + html.escape(title) + """</title>
<style>body{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#18212b}
h1{font-size:1.7rem}details{margin:1rem 0;border:1px solid #c9d1d9;border-radius:6px}
summary{cursor:pointer;font-weight:650;padding:.65rem;background:#f3f6f9}pre{overflow:auto;padding:1rem;margin:0;font:12px/1.45 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
@media print{details{break-inside:avoid}details pre{white-space:pre-wrap}}</style>
<body><h1>""" + html.escape(title) + "</h1>" + "\n".join(body) + "</body></html>\n"
        output_path.write_text(document, encoding="utf-8")
        return output_path
    if format != "pdf":
        raise ValueError("report format must be html or pdf")
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (Paragraph, Preformatted, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError as exc:
        raise RuntimeError('PDF rendering needs `pip install "gwprov[reports]"`') from exc

    title = str(report.get("title") or source_path.stem)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportBody", parent=styles["BodyText"], fontSize=8,
                              leading=10, wordWrap="CJK", alignment=TA_LEFT))
    story = [Paragraph(html.escape(title), styles["Title"]), Spacer(1, 4 * mm)]
    summary = [(str(key), value) for key, value in report.items()
               if not isinstance(value, (dict, list))]
    if summary:
        table = Table([[Paragraph("<b>Field</b>", styles["ReportBody"]),
                        Paragraph("<b>Value</b>", styles["ReportBody"])]] +
                      [[Paragraph(html.escape(key), styles["ReportBody"]),
                        Paragraph(html.escape(str(value)), styles["ReportBody"])]
                       for key, value in summary], colWidths=[45 * mm, 145 * mm], repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
                                   ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#aab4bf")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 5),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 5)]))
        story += [table, Spacer(1, 4 * mm)]
    for key, value in report.items():
        if not isinstance(value, (dict, list)):
            continue
        story.append(Paragraph(html.escape(str(key)), styles["Heading2"]))
        if isinstance(value, list) and len(value) > 100:
            rendered = json.dumps(value[:100], indent=2, ensure_ascii=False, default=str)
            rendered += f"\n… truncated for PDF; {len(value) - 100} additional rows remain in JSON/HTML."
        else:
            rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        wrapped = "\n".join(line[i:i + 110] for line in rendered.splitlines()
                              for i in range(0, max(1, len(line)), 110))
        story.append(Preformatted(wrapped, ParagraphStyle(
            name="ReportCode", fontName="Courier", fontSize=6.5, leading=8,
            leftIndent=2 * mm, rightIndent=2 * mm, wordWrap="CJK")))
        story.append(Spacer(1, 3 * mm))
    doc = SimpleDocTemplate(str(output_path), pagesize=A4, rightMargin=12 * mm,
                            leftMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=title, author="GWProv")
    doc.build(story)
    return output_path
