"""Преобразование отчёта Markdown -> DOCX (python-docx), единое оформление для всех ЛР.

    python lab1/tools/md2docx.py lab2/report.md lab2/report.docx

Поддерживается: титульный лист из front matter (--- title/subtitle/course/author/group/teacher/date ---),
заголовки #..####, абзацы, **жирный**, *курсив*, `код`, [текст](ссылка), списки (- / 1.) с вложенностью,
таблицы в формате pipe, изображения ![подпись](путь) с подписью под рисунком, блоки кода ```, цитаты >,
разрыв страницы строкой <!-- pagebreak -->.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BODY_FONT = "Times New Roman"
MONO_FONT = "Consolas"
INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|(?<![\w*])\*[^*\s][^*]*?\*(?!\w)|(?<!\w)_[^_\s][^_]*?_(?!\w))")


def set_font(run, name=BODY_FONT, size=None, bold=None, italic=None, color=None):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def add_inline(par, text: str, size=None, base_bold=False, base_italic=False):
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            add_inline(par, part[2:-2], size, True, base_italic)
        elif part.startswith("`") and part.endswith("`"):
            r = par.add_run(part[1:-1])
            set_font(r, MONO_FONT, (size or 14) - 2, base_bold, base_italic)
        elif part.startswith("[") and "](" in part:
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", part).groups()
            r = par.add_run(label)
            set_font(r, size=size, bold=base_bold, italic=base_italic, color="1F4E99")
            r.underline = True
            if url != label and not url.startswith("#"):
                r2 = par.add_run(f" ({url})")
                set_font(r2, size=size, bold=base_bold, italic=base_italic)
        elif (part.startswith("*") and part.endswith("*")) or (part.startswith("_") and part.endswith("_")):
            add_inline(par, part[1:-1], size, base_bold, True)
        else:
            r = par.add_run(part)
            set_font(r, size=size, bold=base_bold, italic=base_italic)


def body_par(doc, indent=True, align=WD_ALIGN_PARAGRAPH.JUSTIFY, spacing=1.5):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.alignment = align
    pf.line_spacing = spacing
    pf.space_after = Pt(0)
    pf.space_before = Pt(0)
    pf.first_line_indent = Cm(1.25) if indent else Cm(0)
    return p


def parse_front_matter(lines):
    if not lines or lines[0].strip() != "---":
        return {}, lines
    meta = {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return meta, lines[i + 1:]
        if ":" in lines[i]:
            k, v = lines[i].split(":", 1)
            meta[k.strip()] = v.strip()
    return {}, lines


def title_page(doc, meta):
    def line(text, size=14, bold=False, before=0, align=WD_ALIGN_PARAGRAPH.CENTER):
        p = body_par(doc, indent=False, align=align, spacing=1.15)
        p.paragraph_format.space_before = Pt(before)
        add_inline(p, text, size, bold)
    if meta.get("university"):
        line(meta["university"], 14)
    if meta.get("course"):
        line(f"Курс «{meta['course']}»", 14, before=6)
    line(meta.get("title", "Отчёт"), 18, True, before=150)
    if meta.get("subtitle"):
        line(meta["subtitle"], 16, True, before=10)
    for key in ("author", "group", "teacher"):
        if meta.get(key):
            line(meta[key], 14, before=12 if key == "author" else 0, align=WD_ALIGN_PARAGRAPH.RIGHT)
    if meta.get("date"):
        line(meta["date"], 14, before=150)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def add_table(doc, rows):
    def cells(line):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]
    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]] if len(rows) > 1 and re.match(r"^\s*\|?\s*:?-{2,}", rows[1]) else [cells(r) for r in rows[1:]]
    aligns = []
    if len(rows) > 1:
        for spec in cells(rows[1]):
            aligns.append(WD_ALIGN_PARAGRAPH.RIGHT if spec.endswith(":") and not spec.startswith(":")
                          else WD_ALIGN_PARAGRAPH.CENTER if spec.startswith(":") and spec.endswith(":")
                          else WD_ALIGN_PARAGRAPH.LEFT)
    ncol = len(header)
    t = doc.add_table(rows=1 + len(body), cols=ncol)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ri, row in enumerate([header] + body):
        for ci in range(ncol):
            text = row[ci] if ci < len(row) else ""
            cell = t.cell(ri, ci)
            p = cell.paragraphs[0]
            p.paragraph_format.line_spacing = 1.0
            p.paragraph_format.first_line_indent = Cm(0)
            if ci < len(aligns) and ri > 0:
                p.alignment = aligns[ci]
            add_inline(p, text.replace("<br>", "\n"), 11, base_bold=(ri == 0))
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def add_image(doc, alt, path, base: Path):
    img = (base / path).resolve()
    p = body_par(doc, indent=False, align=WD_ALIGN_PARAGRAPH.CENTER, spacing=1.0)
    if img.exists():
        p.add_run().add_picture(str(img), width=Cm(16))
    else:
        add_inline(p, f"[нет файла изображения: {path}]", 12)
    if alt:
        cap = body_par(doc, indent=False, align=WD_ALIGN_PARAGRAPH.CENTER, spacing=1.0)
        cap.paragraph_format.space_after = Pt(10)
        add_inline(cap, alt, 12)


def convert(md_path: str | Path, docx_path: str | Path) -> None:
    md_path = Path(md_path)
    base = md_path.parent
    meta, lines = parse_front_matter(md_path.read_text(encoding="utf-8").splitlines())
    doc = Document()
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    sec.left_margin, sec.right_margin, sec.top_margin, sec.bottom_margin = Cm(3), Cm(1.5), Cm(2), Cm(2)
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(14)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    if meta:
        title_page(doc, meta)

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if s == "<!-- pagebreak -->":
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            i += 1
            continue
        if s.startswith("<!--"):
            while i < len(lines) and "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            level = len(m.group(1))
            h = doc.add_heading(level=level)
            h.paragraph_format.space_before = Pt(18 if level == 1 else 12)
            h.paragraph_format.space_after = Pt(6)
            h.paragraph_format.keep_with_next = True
            if level == 1:
                h.alignment = WD_ALIGN_PARAGRAPH.CENTER
            add_inline(h, m.group(2), {1: 16, 2: 15, 3: 14, 4: 14}[level], base_bold=True, base_italic=(level == 4))
            for r in h.runs:
                r.font.color.rgb = RGBColor(0, 0, 0)
            i += 1
            continue
        if s.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            p = body_par(doc, indent=False, align=WD_ALIGN_PARAGRAPH.LEFT, spacing=1.0)
            p.paragraph_format.left_indent = Cm(0.5)
            r = p.add_run("\n".join(buf))
            set_font(r, MONO_FONT, 10)
            continue
        m = re.match(r"^!\[(.*?)\]\((.+?)\)\s*$", s)
        if m:
            add_image(doc, m.group(1), m.group(2), base)
            i += 1
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            add_table(doc, rows)
            continue
        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            p = body_par(doc, indent=False)
            p.paragraph_format.left_indent = Cm(1)
            add_inline(p, " ".join(buf), 13, base_italic=True)
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            while i < len(lines):
                m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if not m:
                    if lines[i].startswith("   ") and lines[i].strip():   # продолжение пункта
                        add_inline(p, " " + lines[i].strip())
                        i += 1
                        continue
                    break
                depth = len(m.group(1).replace("\t", "    ")) // 2
                numbered = m.group(2)[0].isdigit()
                p = body_par(doc, indent=False)
                p.paragraph_format.left_indent = Cm(1.25 + 0.75 * depth)
                p.paragraph_format.first_line_indent = Cm(-0.6)
                marker = m.group(2) if numbered else ("–" if depth == 0 else "•")
                add_inline(p, f"{marker} {m.group(3)}")
                i += 1
            continue
        # обычный абзац: склеиваем соседние строки
        buf = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,4}\s|```|!\[|\||>|\s*([-*+]|\d+[.)])\s|<!--)", lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        add_inline(body_par(doc), " ".join(buf))
    doc.save(str(docx_path))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
    print(f"готово: {sys.argv[2]}")
