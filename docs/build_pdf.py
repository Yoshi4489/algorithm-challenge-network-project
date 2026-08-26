"""Build the bilingual CDAP protocol PDF from the two Markdown source documents.

Artifact-only dependency: ReportLab. The arena itself remains standard-library-only.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "CDAP-protocol-spec.pdf"
FONT_DIR = Path(r"C:\Windows\Fonts")
PAGE_WIDTH, PAGE_HEIGHT = A4

NAVY = colors.HexColor("#132238")
BLUE = colors.HexColor("#2364AA")
CYAN = colors.HexColor("#3DA5D9")
PALE = colors.HexColor("#EAF3FA")
INK = colors.HexColor("#17202A")
MUTED = colors.HexColor("#566573")
RULE = colors.HexColor("#B8C7D1")


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("Leela", str(FONT_DIR / "LeelawUI.ttf"), shapable=True))
    pdfmetrics.registerFont(TTFont("Leela-Bold", str(FONT_DIR / "LeelaUIb.ttf"),
                                   shapable=True))
    pdfmetrics.registerFontFamily("Leela", normal="Leela", bold="Leela-Bold")


def styles():
    base = getSampleStyleSheet()
    common = dict(fontName="Leela", textColor=INK, wordWrap="CJK")
    return {
        "title": ParagraphStyle("TitleThai", parent=base["Title"], fontName="Leela-Bold",
                                fontSize=27, leading=34, textColor=colors.white,
                                alignment=TA_CENTER, spaceAfter=8),
        "subtitle": ParagraphStyle("SubtitleThai", fontName="Leela", fontSize=13,
                                   leading=19, textColor=colors.white, alignment=TA_CENTER,
                                   wordWrap="CJK"),
        "h1": ParagraphStyle("H1Thai", parent=base["Heading1"], fontName="Leela-Bold",
                             fontSize=18, leading=24, textColor=NAVY, spaceBefore=14,
                             spaceAfter=8, keepWithNext=True, wordWrap="CJK"),
        "h2": ParagraphStyle("H2Thai", parent=base["Heading2"], fontName="Leela-Bold",
                             fontSize=14, leading=19, textColor=BLUE, spaceBefore=11,
                             spaceAfter=6, keepWithNext=True, wordWrap="CJK"),
        "h3": ParagraphStyle("H3Thai", parent=base["Heading3"], fontName="Leela-Bold",
                             fontSize=11.5, leading=16, textColor=BLUE, spaceBefore=8,
                             spaceAfter=4, keepWithNext=True, wordWrap="CJK"),
        "body": ParagraphStyle("BodyThai", parent=base["BodyText"], **common,
                               fontSize=9.6, leading=14.5, alignment=TA_LEFT,
                               spaceAfter=6),
        "bullet": ParagraphStyle("BulletThai", parent=base["BodyText"], **common,
                                 fontSize=9.4, leading=14, leftIndent=15, firstLineIndent=-8,
                                 bulletIndent=3, spaceAfter=3),
        "quote": ParagraphStyle("QuoteThai", parent=base["BodyText"], **common,
                                fontSize=9.4, leading=14, leftIndent=12, rightIndent=8,
                                borderColor=CYAN, borderWidth=0, borderPadding=7,
                                backColor=PALE, spaceAfter=7),
        "code": ParagraphStyle("Code", fontName="Courier", fontSize=7.8, leading=10.5,
                               textColor=INK, backColor=colors.HexColor("#F4F6F7"),
                               borderColor=RULE, borderWidth=0.4, borderPadding=7,
                               leftIndent=2, rightIndent=2, spaceBefore=4, spaceAfter=8),
        "table_head": ParagraphStyle("TableHead", fontName="Leela-Bold", fontSize=8,
                                     leading=10.5, textColor=colors.white, wordWrap="CJK"),
        "table": ParagraphStyle("TableBody", fontName="Leela", fontSize=7.7,
                                leading=10.2, textColor=INK, wordWrap="CJK"),
        "small": ParagraphStyle("SmallThai", fontName="Leela", fontSize=8.5, leading=12,
                                textColor=MUTED, alignment=TA_CENTER, wordWrap="CJK"),
        "toc": ParagraphStyle("TOCThai", fontName="Leela", fontSize=10.5, leading=17,
                              leftIndent=12, textColor=INK, wordWrap="CJK"),
    }


def inline(text: str) -> str:
    text = html.escape(text.strip())
    text = re.sub(r"`([^`]+)`", r'<font name="Courier" color="#8E2A2A">\1</font>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)
    return text


def table_flowable(rows, style):
    column_count = max(len(row) for row in rows)
    normalized = [row + [""] * (column_count - len(row)) for row in rows]
    wrapped = []
    for row_index, row in enumerate(normalized):
        cell_style = style["table_head"] if row_index == 0 else style["table"]
        wrapped.append([Paragraph(inline(cell), cell_style) for cell in row])
    usable = PAGE_WIDTH - 34 * mm
    if column_count == 2:
        widths = [usable * 0.27, usable * 0.73]
    elif column_count == 3:
        widths = [usable * 0.18, usable * 0.28, usable * 0.54]
    else:
        widths = [usable / column_count] * column_count
    table = Table(wrapped, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFC")]),
    ]))
    return table


def markdown_flows(path: Path, style, skip_title=True):
    lines = path.read_text(encoding="utf-8").splitlines()
    flows = []
    index = 0
    paragraph = []

    def flush():
        if paragraph:
            flows.append(Paragraph(inline(" ".join(part.strip() for part in paragraph)),
                                   style["body"]))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush()
            index += 1
            continue
        if stripped.startswith("```"):
            flush()
            language = stripped[3:].strip()
            index += 1
            code = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            flows.append(Preformatted("\n".join(code), style["code"], maxLineLength=100))
            continue
        if stripped.startswith("|"):
            flush()
            raw_rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    raw_rows.append(cells)
                index += 1
            if raw_rows:
                flows.extend([table_flowable(raw_rows, style), Spacer(1, 7)])
            continue
        if stripped.startswith("#"):
            flush()
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped[level:].strip()
            if level == 1 and skip_title:
                skip_title = False
            else:
                flows.append(Paragraph(inline(heading), style["h1" if level == 1 else
                                                             "h2" if level == 2 else "h3"]))
            index += 1
            continue
        if stripped == "---":
            flush()
            flows.append(Spacer(1, 7))
            index += 1
            continue
        if stripped.startswith("- ["):
            flush()
            checked = stripped.startswith("- [x]") or stripped.startswith("- [X]")
            label = stripped[5:].strip()
            flows.append(Paragraph(inline(label), style["bullet"],
                                   bulletText="[x]" if checked else "[ ]"))
            index += 1
            continue
        if stripped.startswith("- "):
            flush()
            flows.append(Paragraph(inline(stripped[2:]), style["bullet"], bulletText="-"))
            index += 1
            continue
        if re.match(r"^\d+\.\s", stripped):
            flush()
            number, content = stripped.split(".", 1)
            flows.append(Paragraph(inline(content.strip()), style["bullet"],
                                   bulletText=number + "."))
            index += 1
            continue
        if stripped.startswith(">"):
            flush()
            quote = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip()[1:].strip())
                index += 1
            flows.append(Paragraph(inline(" ".join(quote)), style["quote"]))
            continue
        paragraph.append(stripped)
        index += 1
    flush()
    return flows


class NumberedDocTemplate(BaseDocTemplate):
    def __init__(self, filename, **kwargs):
        super().__init__(filename, **kwargs)
        frame = Frame(17 * mm, 18 * mm, PAGE_WIDTH - 34 * mm, PAGE_HEIGHT - 34 * mm,
                      id="body")
        self.addPageTemplates(PageTemplate(id="content", frames=[frame],
                                           onPage=self.decorate_page))

    @staticmethod
    def decorate_page(canvas, doc):
        if doc.page == 1:
            return
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.4)
        canvas.line(17 * mm, PAGE_HEIGHT - 13 * mm, PAGE_WIDTH - 17 * mm,
                    PAGE_HEIGHT - 13 * mm)
        canvas.setFont("Leela", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(17 * mm, PAGE_HEIGHT - 10 * mm, "CDAP/1.0 - Protocol Design")
        canvas.drawRightString(PAGE_WIDTH - 17 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()


def cover(style):
    banner = Table([[Paragraph("CDAP/1.0", style["title"])],
                    [Paragraph("Code Duel Arena Protocol", style["subtitle"])],
                    [Paragraph("Application-Layer Protocol Design", style["subtitle"])]],
                   colWidths=[PAGE_WIDTH - 34 * mm], rowHeights=[43 * mm, 17 * mm, 17 * mm])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0, NAVY),
    ]))
    contents = [
        Spacer(1, 25 * mm), banner, Spacer(1, 18 * mm),
        Paragraph("Computer Networks - Project 1: Socket Programming", style["h2"]),
        Paragraph("สนามแข่งขันเขียนโปรแกรมแบบ real-time ที่ตรวจ correctness พร้อม "
                  "empirical time/space complexity contract", style["body"]),
        Spacer(1, 9 * mm),
        Paragraph("Transport model: TCP authority + UDP display optimization", style["small"]),
        Paragraph("Implementation: Python 3.9+ standard library; optional Docker isolation",
                  style["small"]),
        Spacer(1, 22 * mm),
        Paragraph("Protocol Specification and Threat Model", style["h2"]),
        Paragraph("Bilingual Thai/English technical report", style["small"]),
        PageBreak(),
        Paragraph("Contents / สารบัญ", style["h1"]),
    ]
    for item in (
        "1. Application objective and architecture",
        "2. TCP/UDP transport-layer rationale",
        "3. TCP framing, messages and state machine",
        "4. Status namespaces and UDP datagrams",
        "5. Complexity profiler and sandbox backends",
        "6. Experimental results and demo matrix",
        "Appendix A. Threat model and accepted risks",
    ):
        contents.append(Paragraph(item, style["toc"], bulletText="-"))
    contents.append(PageBreak())
    return contents


def build() -> Path:
    register_fonts()
    style = styles()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    story = cover(style)
    story.extend(markdown_flows(ROOT / "docs" / "CDAP-protocol-spec.md", style))
    story.append(PageBreak())
    story.append(Paragraph("Appendix A - Threat Model / แบบจำลองภัยคุกคาม", style["h1"]))
    story.extend(markdown_flows(ROOT / "docs" / "threat-model.md", style))
    story.append(Spacer(1, 10 * mm))
    story.append(KeepTogether([
        Paragraph("End of report", style["h2"]),
        Paragraph("Source code, experiment scripts and presentation outline are included "
                  "in the same repository.", style["small"]),
    ]))
    doc = NumberedDocTemplate(
        str(OUTPUT), pagesize=A4,
        title="CDAP/1.0 - Code Duel Arena Protocol",
        author="CDAP Computer Networks Project",
        subject="Application-layer protocol design and threat model",
        leftMargin=17 * mm, rightMargin=17 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )
    doc.build(story)
    return OUTPUT


if __name__ == "__main__":
    print(build())
