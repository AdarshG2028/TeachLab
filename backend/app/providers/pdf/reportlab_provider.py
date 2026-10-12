"""Local PDF rendering with ReportLab.

- Markdown (markdown-it-py) -> ReportLab flowables: headings, paragraphs, nested lists, tables, block quotes,
  code blocks (Pygments highlighting), horizontal rules.
- Math: ``$...$`` (inline) and ``$$...$$`` (display) rendered with matplotlib *mathtext* to high-DPI PNGs.
  mathtext covers a large LaTeX subset (fractions, roots, sums, integrals, Greek, accents, sub/superscripts);
  unsupported constructs (e.g. align/matrix environments) fall back to monospace LaTeX source.
- Unicode via the DejaVu fonts bundled with matplotlib (no system font dependency).
- Header metadata block, running header, "Page X of Y" footers, keep-with-next headings.
"""

from __future__ import annotations

import html
import io
import re
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)

from app.core.logging import get_logger
from app.providers.pdf.base import DocumentSpec, PDFProvider

log = get_logger(__name__)

_FONTS_REGISTERED = False
BODY_FONT = "DejaVuSans"
MONO_FONT = "DejaVuSansMono"

DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
INLINE_MATH = re.compile(r"(?<![\\$])\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)")
BLOCK_MATH_ENVS = re.compile(r"\\begin\{(align|matrix|pmatrix|bmatrix|cases|array|eqnarray)")


def _register_fonts() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    import matplotlib

    ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    pdfmetrics.registerFont(TTFont("DejaVuSans", str(ttf / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(ttf / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVuSans-Oblique", str(ttf / "DejaVuSans-Oblique.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVuSans-BoldOblique", str(ttf / "DejaVuSans-BoldOblique.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVuSansMono", str(ttf / "DejaVuSansMono.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVuSansMono-Bold", str(ttf / "DejaVuSansMono-Bold.ttf")))
    from reportlab.pdfbase.pdfmetrics import registerFontFamily

    registerFontFamily("DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold", italic="DejaVuSans-Oblique",
                       boldItalic="DejaVuSans-BoldOblique")
    registerFontFamily("DejaVuSansMono", normal="DejaVuSansMono", bold="DejaVuSansMono-Bold",
                       italic="DejaVuSansMono", boldItalic="DejaVuSansMono-Bold")
    _FONTS_REGISTERED = True


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    ink = colors.HexColor("#1f2933")
    accent = colors.HexColor("#1d4e89")
    s = {
        "title": ParagraphStyle("title", parent=base["Title"], fontName="DejaVuSans-Bold", fontSize=20,
                                leading=25, textColor=ink, spaceAfter=4, alignment=TA_CENTER),
        "subtitle": ParagraphStyle("subtitle", fontName=BODY_FONT, fontSize=11.5, leading=15, alignment=TA_CENTER,
                                   textColor=colors.HexColor("#52606d"), spaceAfter=8),
        "body": ParagraphStyle("body", fontName=BODY_FONT, fontSize=10.5, leading=15, textColor=ink, spaceAfter=6),
        "small": ParagraphStyle("small", fontName=BODY_FONT, fontSize=8.5, leading=11,
                                textColor=colors.HexColor("#52606d")),
        "cell": ParagraphStyle("cell", fontName=BODY_FONT, fontSize=9.5, leading=12.5, textColor=ink),
        "cellh": ParagraphStyle("cellh", fontName="DejaVuSans-Bold", fontSize=9.5, leading=12.5, textColor=ink),
        "quote": ParagraphStyle("quote", fontName="DejaVuSans-Oblique", fontSize=10.5, leading=15, leftIndent=12,
                                textColor=colors.HexColor("#3e4c59"), spaceAfter=6),
        "code": ParagraphStyle("code", fontName=MONO_FONT, fontSize=8.8, leading=11.5, textColor=ink),
        "math_fallback": ParagraphStyle("mathfb", fontName=MONO_FONT, fontSize=9.5, leading=12,
                                        textColor=colors.HexColor("#7b2d26"), spaceAfter=6),
    }
    sizes = {1: (16, 21), 2: (13.5, 18), 3: (12, 16), 4: (11, 14.5), 5: (10.5, 14), 6: (10.5, 14)}
    for lvl, (size, lead) in sizes.items():
        s[f"h{lvl}"] = ParagraphStyle(f"h{lvl}", fontName="DejaVuSans-Bold", fontSize=size, leading=lead,
                                      textColor=accent if lvl <= 2 else ink, spaceBefore=10 if lvl <= 2 else 7,
                                      spaceAfter=5, keepWithNext=True)
    return s


class _NumberedCanvas(rl_canvas.Canvas):
    """Two-pass canvas so footers can show 'Page X of Y'."""

    header_text = ""
    footer_note = ""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []

    def showPage(self) -> None:  # noqa: N802 - ReportLab API
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total: int) -> None:
        w, h = A4
        self.setFont(BODY_FONT, 8)
        self.setFillColor(colors.HexColor("#7b8794"))
        if self._pageNumber > 1 and self.header_text:
            self.drawString(18 * mm, h - 12 * mm, self.header_text[:110])
            self.setStrokeColor(colors.HexColor("#d9e2ec"))
            self.line(18 * mm, h - 13.5 * mm, w - 18 * mm, h - 13.5 * mm)
        self.drawRightString(w - 18 * mm, 10 * mm, f"Page {self._pageNumber} of {total}")
        if self.footer_note:
            self.drawString(18 * mm, 10 * mm, self.footer_note[:100])


class _MathRenderer:
    def __init__(self, tmpdir: Path) -> None:
        self.tmpdir = tmpdir
        self.count = 0
        self.failures: list[str] = []

    def render(self, latex: str, fontsize: float, display: bool) -> tuple[str, float, float, float] | None:
        """Render to PNG; returns (path, width_pt, height_pt, descent_pt) or None if unsupported."""
        if BLOCK_MATH_ENVS.search(latex):
            self.failures.append(latex)
            return None
        from matplotlib import mathtext
        from matplotlib.font_manager import FontProperties

        self.count += 1
        path = self.tmpdir / f"m{self.count}.png"
        expr = "$" + latex.strip().replace("\n", " ") + "$"
        dpi = 300
        try:
            depth = mathtext.math_to_image(expr, str(path), prop=FontProperties(size=fontsize * (1.15 if display
                                                                                               else 1.0)),
                                           dpi=dpi, format="png")
        except Exception:
            self.failures.append(latex)
            return None
        from PIL import Image as PILImage

        with PILImage.open(path) as im:
            wpx, hpx = im.size
        scale = 72.0 / dpi
        return str(path), wpx * scale, hpx * scale, float(depth or 0) * scale


class MarkdownToFlowables:
    def __init__(self, styles: dict[str, ParagraphStyle], math: _MathRenderer, width: float) -> None:
        self.s = styles
        self.math = math
        self.width = width
        self.md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")

    # ---------------- inline

    def _inline_math(self, text: str, fontsize: float) -> str:
        """Replace $..$ in already-escaped text with <img> tags (or monospace fallback)."""

        def sub(m: re.Match) -> str:
            latex = html.unescape(m.group(1))
            r = self.math.render(latex, fontsize, display=False)
            if r is None:
                return f'<font face="{MONO_FONT}" color="#7b2d26">{html.escape(latex)}</font>'
            path, w, h, d = r
            return f'<img src="{path}" width="{w:.2f}" height="{h:.2f}" valign="{-d:.2f}"/>'

        return INLINE_MATH.sub(sub, text)

    def inline(self, tok: Token, fontsize: float = 10.5) -> str:
        out: list[str] = []
        link_closers: list[str] = []
        for child in tok.children or []:
            t = child.type
            if t == "text":
                out.append(html.escape(child.content, quote=False))
            elif t == "softbreak":
                out.append(" ")
            elif t == "hardbreak":
                out.append("<br/>")
            elif t == "code_inline":
                out.append(f'<font face="{MONO_FONT}" backColor="#eef2f7">{html.escape(child.content)}</font>')
            elif t == "strong_open":
                out.append("<b>")
            elif t == "strong_close":
                out.append("</b>")
            elif t == "em_open":
                out.append("<i>")
            elif t == "em_close":
                out.append("</i>")
            elif t == "s_open":
                out.append("<strike>")
            elif t == "s_close":
                out.append("</strike>")
            elif t == "link_open":
                href = child.attrGet("href") or ""
                if href.startswith(("http://", "https://")):
                    out.append(f'<link href="{html.escape(href)}" color="#1d4e89">')
                    link_closers.append("</link>")
                else:
                    out.append("<u>")
                    link_closers.append("</u>")
            elif t == "link_close":
                out.append(link_closers.pop() if link_closers else "")
            elif t == "image":
                alt = child.content or child.attrGet("alt") or "image"
                out.append(f"<i>[Figure: {html.escape(alt)}]</i>")
            elif t == "html_inline":
                out.append(html.escape(child.content))
        return self._inline_math("".join(out), fontsize)

    # ---------------- blocks

    def convert(self, markdown: str) -> list[Flowable]:
        # display math becomes its own paragraph so the block parser keeps it intact
        placeholders: dict[str, str] = {}

        def stash(m: re.Match) -> str:
            key = f"DISPLAYMATH{len(placeholders)}X"
            placeholders[key] = m.group(1)
            return f"\n\n{key}\n\n"

        markdown = DISPLAY_MATH.sub(stash, markdown)
        tokens = self.md.parse(markdown)
        flows, _ = self._blocks(tokens, 0, placeholders, close=None)
        return flows

    def _blocks(self, tokens: list[Token], i: int, ph: dict[str, str], close: str | None
                ) -> tuple[list[Flowable], int]:
        out: list[Flowable] = []
        while i < len(tokens):
            tok = tokens[i]
            if close and tok.type == close:
                return out, i + 1
            if tok.type == "heading_open":
                level = int(tok.tag[1])
                text = self.inline(tokens[i + 1], self.s[f"h{level}"].fontSize)
                out.append(Paragraph(text, self.s[f"h{level}"]))
                i += 3
            elif tok.type == "paragraph_open":
                inline_tok = tokens[i + 1]
                content = inline_tok.content.strip()
                if content in ph:
                    out.append(self._display_math(ph[content]))
                else:
                    out.append(Paragraph(self.inline(inline_tok), self.s["body"]))
                i += 3
            elif tok.type in ("bullet_list_open", "ordered_list_open"):
                ordered = tok.type == "ordered_list_open"
                start = int(tok.attrGet("start") or 1)
                items, i = self._list(tokens, i + 1, ph, "ordered_list_close" if ordered else "bullet_list_close")
                lf = ListFlowable(items, bulletType="1" if ordered else "bullet", start=start if ordered else None,
                                  leftIndent=14, bulletFontName=BODY_FONT, bulletFontSize=9.5,
                                  bulletColor=colors.HexColor("#52606d"))
                out.append(lf)
            elif tok.type in ("fence", "code_block"):
                out.append(self._code(tok.content, (tok.info or "").strip()))
                i += 1
            elif tok.type == "blockquote_open":
                inner, i = self._blocks(tokens, i + 1, ph, "blockquote_close")
                t = Table([[inner]], colWidths=[self.width - 6])
                t.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor("#9fb3c8")),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 10),
                                       ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f7fa"))]))
                out.append(t)
                out.append(Spacer(1, 4))
            elif tok.type == "table_open":
                tbl, i = self._table(tokens, i + 1)
                out.append(tbl)
            elif tok.type == "hr":
                out.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#d9e2ec"),
                                      spaceBefore=4, spaceAfter=6))
                i += 1
            elif tok.type == "html_block":
                out.append(Paragraph(html.escape(tok.content), self.s["body"]))
                i += 1
            else:
                i += 1
        return out, i

    def _list(self, tokens: list[Token], i: int, ph: dict[str, str], close: str) -> tuple[list[ListItem], int]:
        items: list[ListItem] = []
        while i < len(tokens) and tokens[i].type != close:
            if tokens[i].type == "list_item_open":
                inner, i = self._blocks(tokens, i + 1, ph, "list_item_close")
                items.append(ListItem(inner or [Paragraph("", self.s["body"])], leftIndent=14))
            else:
                i += 1
        return items, i + 1

    def _table(self, tokens: list[Token], i: int) -> tuple[Flowable, int]:
        rows: list[list[Flowable]] = []
        header_rows = 0
        row: list[Flowable] = []
        in_head = False
        while i < len(tokens) and tokens[i].type != "table_close":
            t = tokens[i]
            if t.type == "thead_open":
                in_head = True
            elif t.type == "thead_close":
                in_head = False
            elif t.type == "tr_open":
                row = []
            elif t.type == "tr_close":
                rows.append(row)
                if in_head:
                    header_rows += 1
            elif t.type == "inline":
                row.append(Paragraph(self.inline(t, 9.5), self.s["cellh" if in_head else "cell"]))
            i += 1
        ncols = max((len(r) for r in rows), default=1)
        rows = [r + [Paragraph("", self.s["cell"])] * (ncols - len(r)) for r in rows]
        tbl = Table(rows, colWidths=[self.width / ncols] * ncols, repeatRows=header_rows, hAlign="LEFT")
        style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bcccdc")),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header_rows:
            style.append(("BACKGROUND", (0, 0), (-1, header_rows - 1), colors.HexColor("#e4ecf5")))
        tbl.setStyle(TableStyle(style))
        return KeepTogether([tbl, Spacer(1, 6)]) if len(rows) <= 12 else tbl, i + 1

    def _code(self, code: str, lang: str) -> Flowable:
        from pygments import lex
        from pygments.lexers import TextLexer, get_lexer_by_name
        from pygments.styles import get_style_by_name

        caption = None
        if lang.lower() == "mermaid":
            caption = "Diagram source (Mermaid) - rendered in the web preview"
        try:
            lexer = get_lexer_by_name(lang) if lang and lang.lower() != "mermaid" else TextLexer()
        except Exception:
            lexer = TextLexer()
        style = get_style_by_name("friendly")
        # Highlighted markup is built per line so long listings can be cut into table rows: a single table
        # cell taller than a page cannot be split by ReportLab (LayoutError "Splitting error").
        lines: list[list[str]] = [[]]
        for ttype, value in lex(code.rstrip("\n"), lexer):
            st = style.style_for_token(ttype)
            for n, piece in enumerate(value.split("\n")):
                if n:
                    lines.append([])
                if not piece:
                    continue
                esc = html.escape(piece)
                if st.get("color"):
                    esc = f'<font color="#{st["color"]}">{esc}</font>'
                if st.get("bold"):
                    esc = f"<b>{esc}</b>"
                lines[-1].append(esc)
        # Short listings are one row (a row never splits, so it moves whole to the next page, like keep-together);
        # long ones are cut into rows so the table can break between them. No KeepTogether: it emits frame
        # breaks that list items and other containers cannot handle.
        rows_per_chunk = len(lines) if len(lines) < 40 else 20
        chunks = ["\n".join("".join(p) for p in lines[k:k + rows_per_chunk])
                  for k in range(0, len(lines), rows_per_chunk)] or [""]
        rows: list[list[Flowable]] = [[XPreformatted(c, self.s["code"])] for c in chunks]
        if caption:
            rows.insert(0, [Paragraph(f"<i>{caption}</i>", self.s["small"])])
        t = Table(rows, colWidths=[self.width])
        last = len(rows) - 1
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f7fa")),
                               ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9e2ec")),
                               ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, 0), 4), ("BOTTOMPADDING", (0, last), (-1, last), 4)]))
        t.spaceAfter = 6
        return t

    def _display_math(self, latex: str) -> Flowable:
        r = self.math.render(latex, 11.5, display=True)
        if r is None:
            return Paragraph(html.escape(latex.strip()), self.s["math_fallback"])
        path, w, h, _ = r
        if w > self.width:
            h, w = h * self.width / w, self.width
        img = Image(path, width=w, height=h)
        img.hAlign = "CENTER"
        return KeepTogether([Spacer(1, 3), img, Spacer(1, 6)])



class ReportLabPDFProvider(PDFProvider):
    name = "reportlab"

    def __init__(self) -> None:
        _register_fonts()

    def render(self, spec: DocumentSpec) -> bytes:
        styles = _styles()
        buf = io.BytesIO()
        margin = 18 * mm
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=20 * mm,
                                bottomMargin=18 * mm, title=spec.meta.title, author=spec.meta.teacher or "TeachLab",
                                subject=spec.meta.subject, creator="TeachLab")
        width = A4[0] - 2 * margin
        with tempfile.TemporaryDirectory(prefix="teachlab-pdf-") as tmp:
            math = _MathRenderer(Path(tmp))
            conv = MarkdownToFlowables(styles, math, width)
            story: list[Flowable] = []
            if spec.show_header_block:
                story += self._header(spec, styles, width, conv)
            for sec in spec.sections:
                if sec.page_break_before and story:
                    story.append(PageBreak())
                if sec.heading:
                    lvl = max(1, min(4, sec.level))
                    story.append(Paragraph(conv._inline_math(html.escape(sec.heading, quote=False),
                                                             styles[f"h{lvl}"].fontSize), styles[f"h{lvl}"]))
                story += conv.convert(sec.markdown or "")
            if not story:
                story.append(Paragraph("(empty document)", styles["body"]))

            meta = spec.meta

            class Canvas(_NumberedCanvas):
                header_text = meta.title + (f" - {meta.subject}" if meta.subject else "")
                footer_note = meta.footer_note

            doc.build(story, canvasmaker=Canvas)
            if math.failures:
                log.info("pdf_math_fallbacks", count=len(math.failures))
        return buf.getvalue()

    @staticmethod
    def _header(spec: DocumentSpec, s: dict[str, ParagraphStyle], width: float, conv: MarkdownToFlowables
                ) -> list[Flowable]:
        m = spec.meta
        out: list[Flowable] = []
        if m.institution:
            out.append(Paragraph(html.escape(m.institution), s["subtitle"]))
        out.append(Paragraph(html.escape(m.title), s["title"]))
        if m.subtitle:
            out.append(Paragraph(html.escape(m.subtitle), s["subtitle"]))
        fields = [("Subject", m.subject), ("Class", m.class_name), ("Teacher", m.teacher), ("Date", m.date),
                  ("Duration", m.duration), ("Total marks", m.total_marks), *m.extra.items()]
        fields = [(k, v) for k, v in fields if v]
        if fields:
            cells = [Paragraph(f"<b>{html.escape(k)}:</b> {html.escape(str(v))}", s["cell"]) for k, v in fields]
            rows = [cells[i:i + 3] for i in range(0, len(cells), 3)]
            rows = [r + [Paragraph("", s["cell"])] * (3 - len(r)) for r in rows]
            t = Table(rows, colWidths=[width / 3] * 3)
            t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#9fb3c8")),
                                   ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f4f8")),
                                   ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            out += [Spacer(1, 4), t]
        if m.instructions:
            out.append(Spacer(1, 8))
            out.append(Paragraph("<b>Instructions</b>", s["h4"]))
            out.append(ListFlowable([ListItem(Paragraph(conv._inline_math(html.escape(i), 10.5), s["body"]),
                                              leftIndent=14) for i in m.instructions],
                                    bulletType="bullet", leftIndent=14, bulletFontName=BODY_FONT))
        out.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#9fb3c8"), spaceBefore=6,
                              spaceAfter=10))
        return out
