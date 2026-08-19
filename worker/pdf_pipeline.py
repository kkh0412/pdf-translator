from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pymupdf

from .translator import translate_blocks
from .vision_agent import analyze_style, parse_page


PLACEHOLDER_RE = re.compile(r"\[\[MATH_(\d+)\]\]")
DANGEROUS_MATH = re.compile(
    r"\\(?:documentclass|usepackage|input|include|write|openout|read|catcode|csname|newread|newwrite)\b",
    re.I,
)


def _sanitize_unicode(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    return "".join(
        ch for ch in text
        if ch in {"\n", "\t"} or not unicodedata.category(ch).startswith("C")
    )


LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _escape_text(text: str) -> str:
    text = _sanitize_unicode(text)
    return "".join(LATEX_ESCAPES.get(ch, ch) for ch in text)


def _clean_math(latex: str) -> str:
    latex = _sanitize_unicode(latex).strip()
    latex = re.sub(r"^\s*\$\$(.*?)\$\$\s*$", r"\1", latex, flags=re.S)
    latex = re.sub(r"^\s*\\\[(.*?)\\\]\s*$", r"\1", latex, flags=re.S)
    latex = re.sub(r"^\s*\$(.*?)\$\s*$", r"\1", latex, flags=re.S)
    latex = re.sub(
        r"^\s*\\begin\{equation\*?\}(.*?)\\end\{equation\*?\}\s*$",
        r"\1",
        latex,
        flags=re.S,
    ).strip()
    if DANGEROUS_MATH.search(latex):
        raise RuntimeError("Unsafe LaTeX command returned by the vision agent")
    return latex


def _assemble_source(block: dict) -> tuple[str, dict[str, str]]:
    pieces: list[str] = []
    math_map: dict[str, str] = {}
    math_index = 0
    for part in block.get("parts", []):
        ptype = part.get("type")
        content = str(part.get("content", ""))
        if ptype == "math":
            token = f"[[MATH_{math_index}]]"
            math_map[token] = _clean_math(content)
            pieces.append(token)
            math_index += 1
        else:
            pieces.append(content)
    return "".join(pieces).strip(), math_map


def _render_translated_text(text: str, math_map: dict[str, str]) -> str:
    out: list[str] = []
    pos = 0
    for match in PLACEHOLDER_RE.finditer(text):
        out.append(_escape_text(text[pos:match.start()]))
        token = match.group(0)
        formula = math_map.get(token)
        if formula is None:
            raise RuntimeError(f"Unknown math placeholder in translation: {token}")
        out.append(r"\(" + formula + r"\)")
        pos = match.end()
    out.append(_escape_text(text[pos:]))
    return "".join(out)


def _crop_visual_asset(
    doc: pymupdf.Document,
    page_index: int,
    bbox_norm: list[float],
    assets_dir: Path,
    index: int,
) -> Path:
    page = doc[page_index]
    x0, y0, x1, y1 = bbox_norm
    rect = pymupdf.Rect(
        page.rect.width * x0 / 1000.0,
        page.rect.height * y0 / 1000.0,
        page.rect.width * x1 / 1000.0,
        page.rect.height * y1 / 1000.0,
    )
    rect &= page.rect
    if rect.width < 4 or rect.height < 4:
        raise RuntimeError(f"Vision agent produced an invalid figure/table bbox: {bbox_norm}")
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0), clip=rect, alpha=False)
    out = assets_dir / f"visual_{index:04d}.png"
    pix.save(out)
    return out


def reconstruct_document(
    pdf_path: Path,
    work_dir: Path,
    max_pages: int,
) -> tuple[dict, list[dict], list[dict]]:
    doc = pymupdf.open(pdf_path)
    try:
        if doc.page_count == 0:
            raise RuntimeError("PDF has no pages")
        if doc.page_count > max_pages:
            raise RuntimeError(f"This demo accepts at most {max_pages} pages per PDF")
        source_pages = doc.page_count
    finally:
        doc.close()

    print(f"Vision style agent: analyzing rendered source pages ({source_pages} pages total)", flush=True)
    style = analyze_style(pdf_path)

    all_blocks: list[dict] = []
    translation_items: list[dict] = []
    assets_dir = work_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_index = 0

    doc = pymupdf.open(pdf_path)
    try:
        for pno in range(source_pages):
            print(
                f"Vision content agent: reading page image {pno + 1}/{source_pages}",
                flush=True,
            )
            blocks = parse_page(pdf_path, pno, style)
            for local_index, block in enumerate(blocks):
                block = dict(block)
                block_id = f"p{pno}_b{local_index}"
                block["id"] = block_id
                block["page"] = pno

                if block["kind"] in {"figure", "table"}:
                    block["asset"] = _crop_visual_asset(
                        doc, pno, block["bbox"], assets_dir, asset_index
                    )
                    asset_index += 1
                    all_blocks.append(block)
                    continue

                if block["kind"] == "equation":
                    block["equation_latex"] = _clean_math(block["equation_latex"])
                    all_blocks.append(block)
                    continue

                source_text, math_map = _assemble_source(block)
                block["source_text"] = source_text
                block["math_map"] = math_map
                all_blocks.append(block)

                if block.get("translate") and source_text:
                    translation_items.append(
                        {
                            "id": block_id,
                            "kind": block["kind"],
                            "text": source_text,
                        }
                    )
    finally:
        doc.close()

    return style, all_blocks, translation_items


def _hex_color(value: str) -> str:
    value = str(value or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return value.upper()
    return "333333"


def _copy_fonts(work_dir: Path) -> None:
    source = Path(os.getenv("BOOK_FONT_DIR", "/home/runner/.cache/pdf-translator-fonts"))
    needed = [
        "lmroman10-regular.otf",
        "lmroman10-bold.otf",
        "lmroman10-italic.otf",
        "lmroman10-bolditalic.otf",
        "lmsans10-regular.otf",
        "lmsans10-bold.otf",
        "lmsans10-oblique.otf",
        "lmsans10-boldoblique.otf",
        "NanumMyeongjo-Regular.ttf",
        "NanumMyeongjo-Bold.ttf",
        "NanumGothic-Regular.ttf",
        "NanumGothic-Bold.ttf",
    ]
    missing = [x for x in needed if not (source / x).exists()]
    if missing:
        raise RuntimeError(
            f"Typography runtime is missing fonts in {source}: {', '.join(missing)}"
        )
    target = work_dir / "font"
    target.mkdir(parents=True, exist_ok=True)
    for name in needed:
        shutil.copy2(source / name, target / name)


def _font_setup() -> str:
    return r"""
\setmainfont[
  Path={font/},
  UprightFont=lmroman10-regular.otf,
  BoldFont=lmroman10-bold.otf,
  ItalicFont=lmroman10-italic.otf,
  BoldItalicFont=lmroman10-bolditalic.otf
]{lmroman10-regular.otf}
\setsansfont[
  Path={font/},
  UprightFont=lmsans10-regular.otf,
  BoldFont=lmsans10-bold.otf,
  ItalicFont=lmsans10-oblique.otf,
  BoldItalicFont=lmsans10-boldoblique.otf
]{lmsans10-regular.otf}
\setmainhangulfont[
  Path={font/},
  UprightFont=NanumMyeongjo-Regular.ttf,
  BoldFont=NanumMyeongjo-Bold.ttf,
  ItalicFont=NanumMyeongjo-Regular.ttf,
  ItalicFeatures={FakeSlant=0.12},
  BoldItalicFont=NanumMyeongjo-Bold.ttf,
  BoldItalicFeatures={FakeSlant=0.12}
]{NanumMyeongjo-Regular.ttf}
\setsanshangulfont[
  Path={font/},
  UprightFont=NanumGothic-Regular.ttf,
  BoldFont=NanumGothic-Bold.ttf
]{NanumGothic-Regular.ttf}
"""


def _style_wrap(style: str, text: str) -> str:
    if style == "bold":
        return r"\textbf{" + text + "}"
    if style == "italic":
        return r"\textit{" + text + "}"
    if style == "smallcaps":
        return r"\textsc{" + text + "}"
    return text


def _equation_tex(block: dict) -> str:
    body = _clean_math(block.get("equation_latex", ""))
    number = _escape_text(str(block.get("equation_number", "")).strip())
    if not body:
        return ""

    # Vision extraction may return a multiline body. Use aligned for explicit line breaks.
    if r"\\" in body and r"\begin{" not in body:
        body = r"\begin{aligned}" + body + r"\end{aligned}"

    tag = rf"\tag{{{number}}}" if number else ""
    if len(body) > 170 and r"\\" not in body and r"\begin{" not in body:
        return (
            "\\begin{equation}\n"
            "\\resizebox{0.98\\columnwidth}{!}{$\\displaystyle "
            + body
            + "$}"
            + tag
            + "\n\\end{equation}\n"
        )
    return "\\begin{equation}\n" + body + tag + "\n\\end{equation}\n"


def build_latex(
    style: dict,
    blocks: list[dict],
    translations: dict[str, str],
    work_dir: Path,
) -> str:
    _copy_fonts(work_dir)

    columns = int(style.get("columns", 1))
    class_options = "10pt,twocolumn" if columns == 2 else "10pt"
    body_size = float(style.get("body_size_pt", 9.3))
    title_size = float(style.get("title_size_pt", 20.0))
    section_size = float(style.get("section_size_pt", 11.5))
    accent = _hex_color(style.get("title_color", "#333333"))
    gap = float(style.get("column_gap_pt", 18.0))

    preamble = rf"""\documentclass[{class_options}]{{article}}
\usepackage{{fontspec}}
\usepackage{{xetexko}}
\usepackage{{geometry}}
\usepackage{{graphicx}}
\usepackage{{xcolor}}
\usepackage{{amsmath,amssymb}}
\usepackage{{enumitem}}
\geometry{{
  paperwidth={style['page_width_pt']:.2f}pt,
  paperheight={style['page_height_pt']:.2f}pt,
  left={style['left_margin_pt']:.2f}pt,
  right={style['right_margin_pt']:.2f}pt,
  top={style['top_margin_pt']:.2f}pt,
  bottom={style['bottom_margin_pt']:.2f}pt
}}
{_font_setup()}
\definecolor{{SourceAccent}}{{HTML}}{{{accent}}}
\setlength{{\columnsep}}{{{gap:.2f}pt}}
\setlength{{\parindent}}{{{float(style.get('paragraph_indent_em', 1.0)):.2f}em}}
\setlength{{\parskip}}{{0pt}}
\setlength{{\emergencystretch}}{{1.5em}}
\linespread{{{float(style.get('line_spacing', 1.04)):.3f}}}
\AtBeginDocument{{\fontsize{{{body_size:.2f}pt}}{{{body_size*1.18:.2f}pt}}\selectfont}}
\setlist[itemize]{{leftmargin=1.6em,itemsep=0.1em,topsep=0.2em,parsep=0pt}}
\raggedbottom
\makeatletter
\def\ps@sourcepage{{%
  \def\@oddhead{{}}\def\@evenhead{{}}%
  \def\@oddfoot{{\hfill\thepage}}%
  \def\@evenfoot{{\thepage\hfill}}%
}}
\makeatother
\pagestyle{{sourcepage}}
\newcommand{{\SourceSection}}[1]{{%
  \par\vspace{{0.55em}}\noindent
  {{\sffamily\fontsize{{{section_size:.2f}pt}}{{{section_size*1.15:.2f}pt}}\selectfont #1}}
  \par\vspace{{0.25em}}}}
\newcommand{{\SourceSubsection}}[1]{{%
  \par\vspace{{0.42em}}\noindent
  {{\sffamily\bfseries #1}}\par\vspace{{0.15em}}}}
\begin{{document}}
"""

    title_blocks = []
    body_blocks = []
    in_front = True
    for block in blocks:
        if in_front and block["kind"] in {"title", "author", "affiliation", "metadata"}:
            title_blocks.append(block)
        else:
            in_front = False
            body_blocks.append(block)

    def block_text(block: dict) -> str:
        source = block.get("source_text", "")
        translated = translations.get(block["id"], source)
        return _render_translated_text(translated, block.get("math_map", {}))

    front_tex: list[str] = []
    for block in title_blocks:
        kind = block["kind"]
        text = block_text(block)
        if not text:
            continue
        if kind == "title":
            align = style.get("title_alignment", "left")
            begin = r"\begin{center}" if align == "center" else r"\begin{flushleft}"
            end = r"\end{center}" if align == "center" else r"\end{flushleft}"
            family = r"\sffamily" if style.get("title_family") == "sans" else r"\rmfamily"
            front_tex.append(
                begin
                + "\n{"
                + family
                + rf"\color{{SourceAccent}}\fontsize{{{title_size:.2f}pt}}{{{title_size*1.12:.2f}pt}}\selectfont "
                + text
                + "}\n"
                + end
                + "\n\\vspace{0.25em}\n"
            )
        elif kind == "author":
            front_tex.append(
                "\\begin{center}{\\sffamily\\small "
                + text
                + "}\\end{center}\\vspace{0.15em}\n"
            )
        elif kind == "affiliation":
            front_tex.append(
                "\\noindent{\\sffamily\\fontsize{7.6pt}{9.0pt}\\selectfont "
                + text
                + "}\\par\n"
            )
        else:
            front_tex.append(
                "\\noindent{\\sffamily\\scriptsize "
                + text
                + "}\\par\n"
            )

    out = [preamble]
    if title_blocks and columns == 2 and style.get("title_full_width", True):
        out.append("\\twocolumn[{\\begin{@twocolumnfalse}\n")
        out.extend(front_tex)
        out.append("\\vspace{0.55em}\\end{@twocolumnfalse}}]\n")
    else:
        out.extend(front_tex)

    in_items = False

    def close_items():
        nonlocal in_items
        if in_items:
            out.append("\\end{itemize}\n")
            in_items = False

    for block in body_blocks:
        kind = block["kind"]
        if kind in {"footer"}:
            continue

        if kind == "list_item":
            if not in_items:
                out.append("\\begin{itemize}\n")
                in_items = True
            out.append("\\item " + block_text(block) + "\n")
            continue

        close_items()

        if kind == "equation":
            out.append(_equation_tex(block))
            continue

        if kind in {"figure", "table"}:
            asset = Path(block["asset"]).relative_to(work_dir).as_posix()
            full = columns == 2 and block.get("column") == "full"
            env = "figure*" if full else "figure"
            width = r"0.92\textwidth" if full else r"0.96\columnwidth"
            out.append(
                rf"\begin{{{env}}}[!ht]\centering"
                rf"\includegraphics[width={width}]{{\detokenize{{{asset}}}}}"
                rf"\end{{{env}}}" + "\n"
            )
            continue

        text = block_text(block)
        if not text:
            continue
        text = _style_wrap(block.get("style", "normal"), text)

        if kind == "abstract":
            emphasis = style.get("abstract_style", "bold")
            if emphasis == "bold":
                text = r"\textbf{" + text + "}"
            elif emphasis == "italic":
                text = r"\textit{" + text + "}"
            out.append("\\noindent " + text + "\\par\\vspace{0.55em}\n")
        elif kind == "section":
            out.append("\\SourceSection{" + text + "}\n")
        elif kind == "subsection":
            out.append("\\SourceSubsection{" + text + "}\n")
        elif kind == "caption":
            out.append("\\noindent{\\small " + text + "}\\par\\vspace{0.35em}\n")
        elif kind == "reference":
            out.append("\\noindent{\\small " + text + "}\\par\n")
        elif kind in {"author", "affiliation", "metadata"}:
            out.append("\\noindent{\\small " + text + "}\\par\n")
        else:
            out.append(text + "\\par\n")

    close_items()
    out.append("\\end{document}\n")
    return "".join(out)


def compile_latex(tex_source: str, work_dir: Path, output_path: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    tex_path = work_dir / "translated.tex"
    tex_source = _sanitize_unicode(tex_source)
    tex_path.write_text(tex_source, encoding="utf-8")

    engine = shutil.which("xelatex")
    if not engine:
        raise RuntimeError("xelatex was not found in the prepared runtime")

    for run_no in range(2):
        proc = subprocess.run(
            [
                engine,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                tex_path.name,
            ],
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=240,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"XeLaTeX compilation failed on pass {run_no + 1}:\n"
                + proc.stdout[-14000:]
            )

    generated = work_dir / "translated.pdf"
    if not generated.exists():
        raise RuntimeError("XeLaTeX produced no translated.pdf")
    shutil.copy2(generated, output_path)


def _column_guess(pdf_path: Path) -> int:
    doc = pymupdf.open(pdf_path)
    try:
        votes = 0
        considered = 0
        width = float(doc[0].rect.width)
        for pno in range(min(doc.page_count, 6)):
            blocks = [
                b for b in doc[pno].get_text("blocks")
                if len(str(b[4]).strip()) >= 30
            ]
            left = [b for b in blocks if b[2] <= width * 0.58]
            right = [b for b in blocks if b[0] >= width * 0.42]
            if blocks:
                considered += 1
                if len(left) >= 2 and len(right) >= 2:
                    votes += 1
        return 2 if considered and votes / considered >= 0.40 else 1
    finally:
        doc.close()


def process_pdf(
    pdf_path: Path,
    target_language: str,
    work_dir: Path,
    output_path: Path,
    max_pages: int,
) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)

    src_doc = pymupdf.open(pdf_path)
    try:
        source_pages = src_doc.page_count
    finally:
        src_doc.close()

    style, blocks, translation_items = reconstruct_document(
        pdf_path, work_dir, max_pages=max_pages
    )

    print(
        f"Translation agent: translating {len(translation_items)} semantic text blocks; "
        "all math remains protected LaTeX",
        flush=True,
    )
    translations = translate_blocks(translation_items, target_language)

    tex_source = build_latex(style, blocks, translations, work_dir)
    compile_latex(tex_source, work_dir, output_path)

    out_doc = pymupdf.open(output_path)
    try:
        output_pages = out_doc.page_count
    finally:
        out_doc.close()

    if style.get("columns") == 2 and _column_guess(output_path) != 2:
        raise RuntimeError(
            "Style validation failed: source is two-column but generated PDF is not."
        )

    # Reflow is allowed, but an extreme page explosion usually means malformed reconstruction.
    if source_pages >= 3 and output_pages > max(source_pages * 2.4, source_pages + 12):
        raise RuntimeError(
            f"Style/content validation failed: {source_pages} source pages became "
            f"{output_pages} output pages, which is implausibly large."
        )

    return {
        "pages": output_pages,
        "translated_segments": len(translation_items),
        "source_pages": source_pages,
        "columns": style.get("columns"),
        "render_mode": "vision-first-semantic-latex",
    }
