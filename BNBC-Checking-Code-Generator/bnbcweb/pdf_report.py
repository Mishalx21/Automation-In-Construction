"""PDF compliance report — one table per checked rule, listing every
evaluated element (pass and fail) with its measured value against the
BNBC threshold, in the same "Provided vs As Per BNBC" style used by
professional structural calc reports.
"""
from __future__ import annotations

import io
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from bnbcweb.schemas import CheckerInfo, CheckerResult

HEADER_BG = colors.HexColor("#1e3a5f")
ROW_ALT_BG = colors.HexColor("#f5f6f8")
GRID_COLOR = colors.HexColor("#cccccc")
PASS_COLOR = colors.HexColor("#157f3c")
FAIL_COLOR = colors.HexColor("#c01c1c")


def _provided_only(measured: str) -> str:
    # Checkers embed the requirement in `measured` for standalone display;
    # the PDF already has an "As Per BNBC" column, so drop those parts here.
    parts = [p.strip() for p in measured.split(",")]
    kept = [p for p in parts if not p.lower().startswith(("required", "limit="))]
    return ", ".join(kept) or measured


def build_pdf(
    filename: str,
    created_at: str,
    results: list[CheckerResult],
    checkers: dict[str, CheckerInfo],
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        topMargin=16 * mm, bottomMargin=14 * mm,
        leftMargin=14 * mm, rightMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontSize=16, spaceAfter=2)
    meta_style = ParagraphStyle(
        "Meta", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#555555"))
    section_style = ParagraphStyle(
        "Section", parent=styles["Heading2"], fontSize=12,
        spaceBefore=14, spaceAfter=3)
    note_style = ParagraphStyle(
        "Note", parent=styles["Normal"], fontSize=9, spaceAfter=6,
        textColor=colors.HexColor("#333333"))

    story = [
        Paragraph("BNBC COMPLIANCE CHECK REPORT", title_style),
        Paragraph(f"Model: {escape(filename)}", meta_style),
        Paragraph(
            f"Checked: {escape(created_at)}  &middot;  Generated: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            meta_style,
        ),
        Spacer(1, 8 * mm),
    ]

    for r in results:
        info = checkers.get(r.rule_id)
        rule_title = escape(info.title if info else r.rule_id)
        heading = f"{escape(r.rule_id)} &mdash; {rule_title}"
        story.append(Paragraph(heading, section_style))
        if info and info.reference:
            story.append(Paragraph(f"Reference: {escape(info.reference)}", meta_style))

        report = r.report or {}
        summary = r.summary or report.get("summary") or ""
        if summary:
            story.append(Paragraph(escape(summary), note_style))

        if r.error:
            story.append(Paragraph(f"Error: {escape(r.error)}", note_style))
            story.append(Spacer(1, 4 * mm))
            continue

        checks = report.get("checks") or []
        if not checks:
            story.append(Paragraph(
                "No individual elements were evaluated for this rule "
                "(not applicable, or all elements were skipped).",
                note_style,
            ))
            story.append(Spacer(1, 4 * mm))
            continue

        default_criterion = info.title if info else r.rule_id

        # Pivot: one row per (element, storey), one Provided/As-Per-BNBC
        # column pair per distinct criterion this rule evaluates — matches
        # the reference report's per-location, per-named-criterion layout
        # instead of one row per individual check.
        criteria: list[str] = []
        rows: dict[tuple[str, str], dict[str, dict]] = {}
        row_order: list[tuple[str, str]] = []
        for c in checks:
            name = c.get("criterion") or default_criterion
            if name not in criteria:
                criteria.append(name)
            key = (str(c.get("element", "")), str(c.get("storey", "")))
            if key not in rows:
                rows[key] = {}
                row_order.append(key)
            rows[key][name] = c

        header_row1 = ["Element", "Storey"]
        header_row2 = ["", ""]
        for name in criteria:
            header_row1 += [escape(name), "", ""]
            header_row2 += ["Provided", "As Per BNBC", "Result"]
        data = [header_row1, header_row2]

        tag_cells: list[tuple[int, int, bool]] = []  # (col, row, passed)
        for row_i, key in enumerate(row_order, start=2):
            element, storey = key
            row = [
                Paragraph(escape(element), note_style),
                Paragraph(escape(storey), note_style),
            ]
            col = 2
            for name in criteria:
                entry = rows[key].get(name)
                if entry is None:
                    row += ["—", "—", "—"]
                else:
                    passed = entry.get("result") == "pass"
                    row.append(Paragraph(escape(_provided_only(str(entry.get("measured", "")))), note_style))
                    row.append(Paragraph(escape(str(entry.get("threshold", ""))), note_style))
                    row.append("Passed" if passed else "Failed")
                    tag_cells.append((col + 2, row_i, passed))
                col += 3
            data.append(row)

        n_criteria = len(criteria)
        element_w = 46 * mm
        storey_w = 20 * mm
        available = landscape(A4)[0] - 28 * mm - element_w - storey_w
        group_w = available / n_criteria
        col_widths = [element_w, storey_w]
        for _ in criteria:
            col_widths += [group_w * 0.42, group_w * 0.4, group_w * 0.18]

        table = Table(data, colWidths=col_widths, repeatRows=2)
        style = [
            ("SPAN", (0, 0), (0, 1)),
            ("SPAN", (1, 0), (1, 1)),
            ("BACKGROUND", (0, 0), (-1, 1), HEADER_BG),
            ("TEXTCOLOR", (0, 0), (-1, 1), colors.white),
            ("FONTNAME", (0, 0), (-1, 1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("ALIGN", (0, 0), (-1, 1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, GRID_COLOR),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 2), (-1, -1), [colors.white, ROW_ALT_BG]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        col = 2
        for name in criteria:
            style.append(("SPAN", (col, 0), (col + 2, 0)))
            col += 3
        for col_i, row_i, passed in tag_cells:
            style.append(("TEXTCOLOR", (col_i, row_i), (col_i, row_i),
                           PASS_COLOR if passed else FAIL_COLOR))
            style.append(("FONTNAME", (col_i, row_i), (col_i, row_i), "Helvetica-Bold"))
        table.setStyle(TableStyle(style))
        story.append(table)
        story.append(Spacer(1, 6 * mm))

    doc.build(story)
    return buf.getvalue()
