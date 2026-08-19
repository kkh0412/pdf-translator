from __future__ import annotations

import concurrent.futures
import os
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Callable

import pymupdf

from .translator import translate_blocks
from .vision_agent import (
    GeminiVisionError,
    _decode_math_transport,
    repair_math_formula,
    analyze_document,
    parse_pages,
)


PLACEHOLDER_RE = re.compile(r"\[\[MATH_(\d+)\]\]")
DANGEROUS_MATH = re.compile(
    r"\\(?:documentclass|usepackage|input|include|write|openout|read|catcode|csname|newread|newwrite)\b",
    re.I,
)


def _sanitize_unicode(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    return "".join(
        ch
        for ch in text
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
    # Decode § transport / repair JSON controls before generic sanitation.
    # This is the critical ordering: sanitation must not delete the evidence first.
    latex = _decode_math_transport(latex)
    latex = _sanitize_unicode(latex).strip()

    unicode_math = {
        "−": "-",
        "∈": r"\in ",
        "∉": r"\notin ",
        "≤": r"\leq ",
        "≥": r"\geq ",
        "≠": r"\neq ",
        "×": r"\times ",
        "·": r"\cdot ",
        "∞": r"\infty ",
        "→": r"\to ",
        "↦": r"\mapsto ",
        "∅": r"\varnothing ",
        "∩": r"\cap ",
        "∪": r"\cup ",
    }
    for source, target in unicode_math.items():
        latex = latex.replace(source, target)

    # Standalone \t is not a valid spacing command. Vision occasionally emits
    # long runs like "\t\t\t := ...". Genuine \theta / \textsf / \times
    # do not match this word-boundary pattern.
    latex = re.sub(r"(?:\\t\b\s*)+", " ", latex)

    # Vision can emit a multi-line alignment separator as a SINGLE backslash:
    #
    #     ... D(...) \ &= ...
    #
    # In TeX a line break is "\\", so normalize only the narrow pattern
    # "single backslash + whitespace + &". Literal \& remains untouched.
    latex = re.sub(r"(?<!\\)\\\s+(?=&)", r"\\\\ ", latex)

    # Defensive cleanup for isolated legacy one-letter transport prefixes.
    latex = re.sub(r"(?:\\r\b\s*)+", " ", latex)
    latex = re.sub(r"(?:\\f\b\s*)+", " ", latex)
    latex = re.sub(r"^\s*\$\$(.*?)\$\$\s*$", r"\1", latex, flags=re.S)
    latex = re.sub(r"^\s*\\\[(.*?)\\\]\s*$", r"\1", latex, flags=re.S)
    latex = re.sub(r"^\s*\$(.*?)\$\s*$", r"\1", latex, flags=re.S)
    latex = re.sub(
        r"^\s*\\begin\{equation\*?\}(.*?)\\end\{equation\*?\}\s*$",
        r"\1",
        latex,
        flags=re.S,
    )
    latex = re.sub(
        r"^\s*\\begin\{aligned\}(.*?)\\end\{aligned\}\s*$",
        r"\1",
        latex,
        flags=re.S,
    ).strip()

    if DANGEROUS_MATH.search(latex):
        raise RuntimeError("Unsafe LaTeX command returned by the vision agent")

    # Normalize notation aliases that are not guaranteed to exist in a minimal
    # TeX installation. This keeps the mathematical meaning while avoiding
    # package-specific commands emitted by the vision model.
    replacements = (
        (r"\\coloneqq\b", r"\\mathrel{:=}"),
        (r"\\coloneq\b", r"\\mathrel{:=}"),
        (r"\\eqqcolon\b", r"\\mathrel{=:}"),
        (r"\\eqcolon\b", r"\\mathrel{=:}"),
    )
    for pattern, replacement in replacements:
        latex = re.sub(pattern, replacement, latex)

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

        # Inline mathematics is always real LaTeX. Superscripts/subscripts
        # reconstructed by the vision agent remain untouched here.
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
        raise RuntimeError(
            f"Vision agent produced an invalid figure/table bbox: {bbox_norm}"
        )

    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(2.0, 2.0),
        clip=rect,
        alpha=False,
    )
    out = assets_dir / f"visual_{index:04d}.png"
    pix.save(out)
    return out


def _page_batches(page_count: int, pages_per_call: int) -> list[list[int]]:
    return [
        list(range(start, min(page_count, start + pages_per_call)))
        for start in range(0, page_count, pages_per_call)
    ]


def reconstruct_document(
    pdf_path: Path,
    target_language: str,
    work_dir: Path,
    max_pages: int,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[dict, dict, list[dict], list[dict]]:
    doc = pymupdf.open(pdf_path)
    try:
        if doc.page_count == 0:
            raise RuntimeError("PDF has no pages")
        if doc.page_count > max_pages:
            raise RuntimeError(
                f"This demo accepts at most {max_pages} pages per PDF"
            )
        source_pages = doc.page_count
    finally:
        doc.close()

    print(
        f"Document pre-scan: style + field + terminology strategy "
        f"({source_pages} pages total)",
        flush=True,
    )
    if progress_callback:
        progress_callback(
            8,
            "문서 분야·스타일·전문용어 번역 전략을 먼저 분석하고 있습니다.",
        )

    style, strategy = analyze_document(pdf_path, target_language)

    if progress_callback:
        field_label = " / ".join(
            x for x in (
                str(strategy.get("field", "")).strip(),
                str(strategy.get("subfield", "")).strip(),
            )
            if x
        )
        progress_callback(
            15,
            f"사전 분석 완료 · {field_label or '문서 구조 분석 준비'}",
        )

    pages_per_call = max(
        1, min(3, int(os.getenv("VISION_PAGES_PER_CALL", "2")))
    )
    workers = max(1, min(3, int(os.getenv("VISION_WORKERS", "2"))))
    batches = _page_batches(source_pages, pages_per_call)

    print(
        f"Vision reconstruction plan: {source_pages} pages -> "
        f"{len(batches)} calls, up to {pages_per_call} pages/call, workers={workers}",
        flush=True,
    )

    def parse_batch(indices: list[int]) -> dict[int, list[dict]]:
        try:
            return parse_pages(pdf_path, indices, style, strategy)
        except Exception as exc:
            if len(indices) == 1:
                raise
            print(
                f"Vision batch {[i + 1 for i in indices]} failed; "
                f"retrying pages individually: {exc}",
                flush=True,
            )
            merged: dict[int, list[dict]] = {}
            for index in indices:
                merged.update(parse_pages(pdf_path, [index], style, strategy))
            return merged

    page_results: dict[int, list[dict]] = {}
    completed_pages = 0

    def report_vision_progress(done_pages: int) -> None:
        if not progress_callback:
            return
        fraction = done_pages / max(1, source_pages)
        percent = 15 + int(round(40 * fraction))
        progress_callback(
            min(55, percent),
            f"페이지 구조·수식·컬럼 분석 중 · {done_pages}/{source_pages}페이지",
        )

    if workers == 1 or len(batches) <= 1:
        for batch in batches:
            page_results.update(parse_batch(batch))
            completed_pages += len(batch)
            report_vision_progress(completed_pages)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(parse_batch, batch): batch for batch in batches
            }
            for future in concurrent.futures.as_completed(future_map):
                batch = future_map[future]
                page_results.update(future.result())
                completed_pages += len(batch)
                report_vision_progress(completed_pages)

    missing_pages = [
        index for index in range(source_pages) if index not in page_results
    ]
    if missing_pages:
        raise RuntimeError(
            f"Vision reconstruction missed pages: "
            f"{[index + 1 for index in missing_pages]}"
        )

    all_blocks: list[dict] = []
    translation_items: list[dict] = []
    assets_dir = work_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_index = 0

    doc = pymupdf.open(pdf_path)
    try:
        for pno in range(source_pages):
            for local_index, original_block in enumerate(page_results[pno]):
                block = dict(original_block)
                block_id = f"p{pno}_b{local_index}"
                block["id"] = block_id
                block["page"] = pno
                block["flow_columns"] = max(
                    1, min(3, int(block.get("flow_columns", style["columns"])))
                )

                if block["kind"] in {"figure", "table"}:
                    block["asset"] = _crop_visual_asset(
                        doc,
                        pno,
                        block["bbox"],
                        assets_dir,
                        asset_index,
                    )
                    asset_index += 1
                    all_blocks.append(block)
                    continue

                if block["kind"] == "equation":
                    block["equation_latex"] = _clean_math(
                        block.get("equation_latex", "")
                    )
                    block["equation_lines"] = [
                        _clean_math(line)
                        for line in block.get("equation_lines", [])
                        if str(line).strip()
                    ]
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

    return style, strategy, all_blocks, translation_items


def _hex_color(value: str) -> str:
    value = str(value or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return value.upper()
    return "333333"


def _copy_fonts(work_dir: Path) -> None:
    source = Path(
        os.getenv(
            "BOOK_FONT_DIR",
            "/home/runner/.cache/pdf-translator-fonts",
        )
    )
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
    missing = [name for name in needed if not (source / name).exists()]
    if missing:
        raise RuntimeError(
            f"Typography runtime is missing fonts in {source}: "
            + ", ".join(missing)
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


_TOP_LEVEL_BREAK_TOKENS = [
    r"\Longleftrightarrow",
    r"\Leftrightarrow",
    r"\Longrightarrow",
    r"\Rightarrow",
    r"\Longleftarrow",
    r"\Leftarrow",
    r"\iff",
    r"\coloneqq",
    r"\equiv",
    r"\approx",
    r"\simeq",
    r"\neq",
    r"\leq",
    r"\geq",
    ":=",
    "=",
    r"\pm",
    r"\mp",
    r"\oplus",
    r"\otimes",
    "+",
    "-",
]


def _top_level_breaks(math: str) -> list[tuple[int, str]]:
    """Return safe operator positions outside {...} and paired delimiters."""
    breaks: list[tuple[int, str]] = []
    brace_depth = 0
    paren_depth = 0
    i = 0

    while i < len(math):
        char = math[i]

        if char == "{":
            brace_depth += 1
            i += 1
            continue
        if char == "}":
            brace_depth = max(0, brace_depth - 1)
            i += 1
            continue

        if brace_depth == 0:
            if char in "([":
                paren_depth += 1
                i += 1
                continue
            if char in ")]":
                paren_depth = max(0, paren_depth - 1)
                i += 1
                continue

        if brace_depth == 0 and paren_depth == 0:
            matched = False
            for token in _TOP_LEVEL_BREAK_TOKENS:
                if math.startswith(token, i):
                    # Avoid interpreting a unary leading minus as a break.
                    if token == "-" and i < 4:
                        continue
                    breaks.append((i, token))
                    i += len(token)
                    matched = True
                    break
            if matched:
                continue

        # Skip over TeX command names so their internal letters are untouched.
        if char == "\\" and i + 1 < len(math) and math[i + 1].isalpha():
            i += 2
            while i < len(math) and math[i].isalpha():
                i += 1
            continue

        i += 1

    return breaks


def _auto_break_equation(math: str, target: int) -> list[str]:
    math = " ".join(math.split())
    if len(math) <= target:
        return [math]

    breaks = _top_level_breaks(math)
    if not breaks:
        return [math]

    lines: list[str] = []
    start = 0

    while len(math) - start > target:
        candidates = [
            (pos, token)
            for pos, token in breaks
            if start + max(18, int(target * 0.52)) <= pos <= start + target
        ]

        if not candidates:
            candidates = [
                (pos, token)
                for pos, token in breaks
                if start + 16 <= pos <= start + int(target * 1.20)
            ]

        if not candidates:
            break

        pos, token = max(candidates, key=lambda item: item[0])
        segment = math[start:pos].strip()

        if segment:
            lines.append(segment)

        # Keep the relation/operator at the start of the continuation line.
        start = pos

        # Avoid an infinite loop.
        if len(lines) > 8:
            break

    tail = math[start:].strip()
    if tail:
        lines.append(tail)

    if len(lines) <= 1:
        return [math]

    return lines


def _align_math_lines(lines: list[str]) -> list[str]:
    aligned: list[str] = []

    relation_pattern = re.compile(
        r"(\\Longleftrightarrow|\\Leftrightarrow|\\Longrightarrow|"
        r"\\Rightarrow|\\Longleftarrow|\\Leftarrow|\\iff|\\coloneqq|"
        r"\\equiv|\\approx|\\simeq|\\neq|\\leq|\\geq|:=|=)"
    )

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue

        if "&" in line:
            aligned.append(line)
            continue

        if index > 0:
            if re.match(
                r"^(?:=|:=|\+|-|\\pm|\\mp|\\oplus|\\otimes|"
                r"\\Rightarrow|\\Leftrightarrow|\\iff|\\leq|\\geq|\\neq)",
                line,
            ):
                aligned.append(r"&\quad " + line)
                continue

        match = relation_pattern.search(line)
        if match:
            pos = match.start()
            aligned.append(line[:pos] + "&" + line[pos:])
        else:
            aligned.append(line)

    return aligned


def _equation_tex(block: dict) -> str:
    body = _clean_math(block.get("equation_latex", ""))
    supplied_lines = [
        _clean_math(line)
        for line in block.get("equation_lines", [])
        if str(line).strip()
    ]
    number = _escape_text(str(block.get("equation_number", "")).strip())

    if not body and supplied_lines:
        body = " ".join(supplied_lines)
    if not body:
        return ""

    flow_columns = max(1, min(3, int(block.get("flow_columns", 1))))
    is_full = block.get("column") == "full"

    # Approximate target TeX length for one visual line. Narrower local flows
    # get more aggressive breaking.
    if is_full or flow_columns == 1:
        target = 118
    elif flow_columns == 2:
        target = 70
    else:
        target = 52

    if supplied_lines:
        lines = supplied_lines
        # A vision-provided line can still be too wide. Subdivide only that line.
        refined: list[str] = []
        for line in lines:
            refined.extend(_auto_break_equation(line, target))
        lines = refined
    elif r"\\" in body and r"\begin{" not in body:
        lines = [part.strip() for part in body.split(r"\\") if part.strip()]
    else:
        lines = _auto_break_equation(body, target)

    tag = rf"\tag{{{number}}}" if number else ""

    needs_aligned = len(lines) > 1 or any("&" in line for line in lines)

    if needs_aligned:
        lines = _align_math_lines(lines)
        return (
            "\\begin{equation}\n"
            "\\begin{aligned}\n"
            + " \\\\\n".join(lines)
            + "\n\\end{aligned}"
            + tag
            + "\n\\end{equation}\n"
        )

    single = lines[0]

    # Last resort only: if there is no safe mathematical breakpoint at all,
    # scale that indivisible expression instead of letting it leave the column.
    if len(single) > int(target * 1.30):
        return (
            "\\begin{equation}\n"
            "\\resizebox{0.98\\linewidth}{!}{$\\displaystyle "
            + single
            + "$}"
            + tag
            + "\n\\end{equation}\n"
        )

    return (
        "\\begin{equation}\n"
        + single
        + tag
        + "\n\\end{equation}\n"
    )



def _math_source_crop(pdf_path: Path, block: dict, dpi: int = 180) -> bytes | None:
    """Render a padded crop around the source block for formula repair."""
    bbox = block.get("bbox") or []
    page_index = int(block.get("page", -1))
    if len(bbox) != 4 or page_index < 0:
        return None

    try:
        doc = pymupdf.open(pdf_path)
        try:
            if page_index >= doc.page_count:
                return None
            page = doc[page_index]
            x0, y0, x1, y1 = [float(x) for x in bbox]
            # normalized 0..1000 -> page points, with enough padding to expose
            # superscripts/subscripts and nearby equation numbers.
            rect = pymupdf.Rect(
                page.rect.width * x0 / 1000.0,
                page.rect.height * y0 / 1000.0,
                page.rect.width * x1 / 1000.0,
                page.rect.height * y1 / 1000.0,
            )
            pad_x = max(8.0, rect.width * 0.06)
            pad_y = max(6.0, rect.height * 0.22)
            rect = pymupdf.Rect(
                rect.x0 - pad_x, rect.y0 - pad_y,
                rect.x1 + pad_x, rect.y1 + pad_y,
            ) & page.rect
            if rect.width < 5 or rect.height < 5:
                return None
            scale = dpi / 72.0
            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale),
                clip=rect,
                alpha=False,
            )
            return pix.tobytes("jpeg", jpg_quality=90)
        finally:
            doc.close()
    except Exception as exc:
        print(f"Math repair crop unavailable: {exc}", flush=True)
        return None


def _last_control_sequence_from_error(output: str) -> str | None:
    """Best-effort extraction of the undefined TeX control word."""
    tail = output[-2500:]
    if "Undefined control sequence" not in tail:
        return None

    # The undefined command is normally the final control word on the reported
    # l.<n> source fragment. This avoids mistaking earlier valid commands.
    source_lines = re.findall(r"(?:^|\n)l\.\d+\s+([^\n]+)", tail)
    candidates_source = source_lines[-1] if source_lines else tail
    commands = re.findall(r"\\([A-Za-z]+)", candidates_source)
    return commands[-1] if commands else None


def _repair_simple_undefined_script(formula: str, command: str | None) -> str | None:
    """Safely expand an undefined short textual script without guessing math.

    Example: ^{\\fn} cannot compile, but visually it can only typeset the
    letters 'fn' if that was intended. Convert this narrow shape to
    ^{\\mathrm{fn}}. Unknown commands elsewhere are *not* guessed and are sent
    to the source-image repair agent instead.
    """
    if not command or not re.fullmatch(r"[A-Za-z]{1,5}", command):
        return None

    pattern = re.compile(r"([_^])\{\\" + re.escape(command) + r"\}")
    if not pattern.search(formula):
        return None

    repaired = pattern.sub(
        lambda m: m.group(1) + r"{\mathrm{" + command + "}}",
        formula,
    )
    return repaired if repaired != formula else None


def _math_preflight_preamble() -> str:
    return r"""\documentclass[10pt]{article}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\providecommand{\coloneq}{\mathrel{:=}}
\providecommand{\coloneqq}{\mathrel{:=}}
\providecommand{\eqqcolon}{\mathrel{=:}}
\providecommand{\eqcolon}{\mathrel{=:}}
\providecommand{\Tr}{\operatorname{Tr}}
\providecommand{\rank}{\operatorname{rank}}
\providecommand{\supp}{\operatorname{supp}}
\providecommand{\diag}{\operatorname{diag}}
\providecommand{\ket}[1]{\left\lvert #1\right\rangle}
\providecommand{\bra}[1]{\left\langle #1\right\rvert}
\providecommand{\braket}[2]{\left\langle #1\middle|#2\right\rangle}
\providecommand{\mel}[3]{\left\langle #1\middle|#2\middle|#3\right\rangle}
\providecommand{\dd}{\mathrm{d}}
\begin{document}
"""


def preflight_math_blocks(
    blocks: list[dict],
    work_dir: Path,
    pdf_path: Path,
    progress_callback: Callable[[int, str], None] | None = None,
) -> None:
    """Preflight every formula independently and repair failures source-faithfully.

    Why independently? A single combined TeX document stops at the first bad
    formula, forcing the whole document to be recompiled repeatedly. Here all
    formulas are compiled in parallel, all failures are collected at once, and
    only the failed formulas are repaired/retested.
    """
    records: list[dict] = []

    for block in blocks:
        block_id = str(block.get("id", "?"))

        if block.get("kind") == "equation":
            formula = _clean_math(block.get("equation_latex", ""))
            if formula:
                block["equation_latex"] = formula
                block["equation_lines"] = [
                    _clean_math(line)
                    for line in block.get("equation_lines", [])
                    if str(line).strip()
                ]
                records.append({
                    "label": f"{block_id}:display",
                    "formula": formula,
                    "block": block,
                    "kind": "display",
                    "key": None,
                    "repair_count": 0,
                })

        for token, formula in (block.get("math_map") or {}).items():
            cleaned = _clean_math(formula)
            if cleaned:
                block["math_map"][token] = cleaned
                records.append({
                    "label": f"{block_id}:{token}",
                    "formula": cleaned,
                    "block": block,
                    "kind": "inline",
                    "key": token,
                    "repair_count": 0,
                })

    if not records:
        print("Math preflight: no formulas to check.", flush=True)
        return

    preflight_dir = work_dir / "math-preflight"
    if preflight_dir.exists():
        shutil.rmtree(preflight_dir)
    preflight_dir.mkdir(parents=True, exist_ok=True)

    engine = shutil.which("xelatex")
    if not engine:
        raise RuntimeError("xelatex was not found for math preflight")

    def record_tex(record: dict) -> str:
        if record["kind"] == "display":
            return _equation_tex(record["block"])
        return "\\noindent Inline test: \\(" + record["formula"] + "\\)\\par\n"

    def compile_one(index: int, record: dict) -> tuple[int, bool, str, str]:
        tex_path = preflight_dir / f"formula_{index:04d}.tex"
        tex = (
            _math_preflight_preamble().replace("\\begin{document}\n", "")
            + "\\begin{document}\n"
            + f"\\typeout{{PDFTRANSLATOR-MATH-{index}: {record['label']}}}\n"
            + record_tex(record)
            + "\\end{document}\n"
        )
        tex_path.write_text(tex, encoding="utf-8")
        proc = subprocess.run(
            [
                engine,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                tex_path.name,
            ],
            cwd=preflight_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
        )
        return index, proc.returncode == 0, proc.stdout, tex

    max_workers = max(1, min(6, int(os.getenv("MATH_PREFLIGHT_WORKERS", "4"))))

    def compile_subset(indices: list[int]) -> dict[int, tuple[bool, str, str]]:
        results: dict[int, tuple[bool, str, str]] = {}
        if max_workers == 1 or len(indices) <= 1:
            for idx in indices:
                _, ok, out, tex = compile_one(idx, records[idx])
                results[idx] = (ok, out, tex)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                future_map = {ex.submit(compile_one, idx, records[idx]): idx for idx in indices}
                for future in concurrent.futures.as_completed(future_map):
                    idx, ok, out, tex = future.result()
                    results[idx] = (ok, out, tex)
        return results

    all_indices = list(range(len(records)))
    results = compile_subset(all_indices)
    failing = [idx for idx in all_indices if not results[idx][0]]

    if progress_callback:
        passed = len(records) - len(failing)
        progress_callback(
            57,
            f"수식 사전 검사 · {passed}/{len(records)}개 통과"
            + (f" · {len(failing)}개 자동 복구 중" if failing else ""),
        )

    # Every failing formula gets its own repair budget. There is intentionally
    # no document-wide 'two repairs only' cap anymore.
    for round_no in range(2):
        if not failing:
            break

        to_recompile: list[int] = []
        for idx in failing:
            record = records[idx]
            ok, compiler_output, _tex = results[idx]
            if ok:
                continue

            record["repair_count"] += 1
            command = _last_control_sequence_from_error(compiler_output)

            # Deterministic, semantically conservative repair first.
            repaired = _repair_simple_undefined_script(record["formula"], command)
            method = None
            if repaired:
                method = f"undefined script \\{command} -> \\mathrm{{{command}}}"
            else:
                # Anything less obvious is reconstructed from the original PDF crop.
                crop = _math_source_crop(pdf_path, record["block"])
                try:
                    repaired = _clean_math(
                        repair_math_formula(
                            record["formula"],
                            label=record["label"],
                            source_image=crop,
                            compiler_error=compiler_output,
                        )
                    )
                    method = "source-image Gemini repair"
                except Exception as exc:
                    print(
                        f"Math repair failed for {record['label']}: {exc}",
                        flush=True,
                    )
                    continue

            print(
                f"Math preflight repair {record['label']}: {method}",
                flush=True,
            )

            if record["kind"] == "display":
                record["block"]["equation_latex"] = repaired
                # Discard stale line hints after full-expression repair.
                record["block"]["equation_lines"] = []
            else:
                record["block"]["math_map"][record["key"]] = repaired

            record["formula"] = repaired
            to_recompile.append(idx)

        if not to_recompile:
            break

        retry_results = compile_subset(to_recompile)
        results.update(retry_results)
        failing = [idx for idx in failing if not results[idx][0]]

        if progress_callback:
            progress_callback(
                57,
                f"수식 사전 검사 복구 {round_no + 1}/2 · "
                f"남은 오류 {len(failing)}개",
            )

    if failing:
        details: list[str] = []
        for idx in failing[:8]:
            record = records[idx]
            ok, output, tex = results[idx]
            details.append(
                f"[{record['label']}]\n"
                f"Formula: {record['formula']}\n"
                f"Rendered: {record_tex(record)}\n"
                f"Error: {output[-1800:]}"
            )

        raise RuntimeError(
            f"Math preflight still has {len(failing)} unrepaired formula(s) after "
            "source-aware repair.\n\n" + "\n\n".join(details)
        )

    print(
        f"Math preflight: all {len(records)} formulas compiled successfully.",
        flush=True,
    )


def build_latex(
    style: dict,
    blocks: list[dict],
    translations: dict[str, str],
    work_dir: Path,
) -> str:
    _copy_fonts(work_dir)

    # User-facing readability adjustment:
    # slightly smaller body type, slightly more leading than the detected source.
    detected_body = float(style.get("body_size_pt", 9.3))
    body_size = max(7.6, min(10.2, detected_body * 0.94))
    leading_factor = max(
        1.24,
        min(1.32, float(style.get("line_spacing", 1.04)) * 1.20),
    )
    baseline = body_size * leading_factor

    title_size = float(style.get("title_size_pt", 20.0))
    section_size = float(style.get("section_size_pt", 11.5)) * 0.97
    accent = _hex_color(style.get("title_color", "#333333"))
    gap = float(style.get("column_gap_pt", 18.0))

    preamble = rf"""\documentclass[10pt]{{article}}
\usepackage{{fontspec}}
\usepackage{{xetexko}}
\usepackage{{geometry}}
\usepackage{{graphicx}}
\usepackage{{xcolor}}
\usepackage{{amsmath,amssymb}}
\usepackage{{enumitem}}
\usepackage{{multicol}}
% Compatibility aliases: avoid extra packages for common AI/source notation.
\providecommand{{\coloneq}}{{\mathrel{{:=}}}}
\providecommand{{\coloneqq}}{{\mathrel{{:=}}}}
\providecommand{{\eqqcolon}}{{\mathrel{{=:}}}}
\providecommand{{\eqcolon}}{{\mathrel{{=:}}}}
\providecommand{{\Tr}}{{\operatorname{{Tr}}}}
\providecommand{{\rank}}{{\operatorname{{rank}}}}
\providecommand{{\supp}}{{\operatorname{{supp}}}}
\providecommand{{\diag}}{{\operatorname{{diag}}}}
\providecommand{{\ket}}[1]{{\left\lvert #1\right\rangle}}
\providecommand{{\bra}}[1]{{\left\langle #1\right\rvert}}
\providecommand{{\braket}}[2]{{\left\langle #1\middle|#2\right\rangle}}
\providecommand{{\mel}}[3]{{\left\langle #1\middle|#2\middle|#3\right\rangle}}
\providecommand{{\dd}}{{\mathrm{{d}}}}
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
\setlength{{\emergencystretch}}{{1.7em}}
\setlength{{\multicolsep}}{{0.35em}}
\setlength{{\premulticols}}{{0.2em}}
\setlength{{\postmulticols}}{{0.2em}}
\AtBeginDocument{{\fontsize{{{body_size:.2f}pt}}{{{baseline:.2f}pt}}\selectfont}}
\setlist[itemize]{{leftmargin=1.5em,itemsep=0.12em,topsep=0.25em,parsep=0pt}}
\allowdisplaybreaks[2]
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
  \par\vspace{{0.52em}}\noindent
  {{\sffamily\fontsize{{{section_size:.2f}pt}}{{{section_size*1.18:.2f}pt}}\selectfont #1}}
  \par\vspace{{0.25em}}}}
\newcommand{{\SourceSubsection}}[1]{{%
  \par\vspace{{0.40em}}\noindent
  {{\sffamily\bfseries #1}}\par\vspace{{0.16em}}}}
\begin{{document}}
"""

    title_blocks: list[dict] = []
    body_blocks: list[dict] = []
    in_front = True

    for block in blocks:
        if in_front and block["kind"] in {
            "title",
            "author",
            "affiliation",
            "metadata",
        }:
            title_blocks.append(block)
        else:
            in_front = False
            body_blocks.append(block)

    def block_text(block: dict) -> str:
        source = block.get("source_text", "")
        translated = translations.get(block["id"], source)
        return _render_translated_text(
            translated,
            block.get("math_map", {}),
        )

    out = [preamble]

    for block in title_blocks:
        kind = block["kind"]
        text = block_text(block)

        if not text:
            continue

        if kind == "title":
            align = style.get("title_alignment", "left")
            begin = (
                r"\begin{center}"
                if align == "center"
                else r"\begin{flushleft}"
            )
            end = (
                r"\end{center}"
                if align == "center"
                else r"\end{flushleft}"
            )
            family = (
                r"\sffamily"
                if style.get("title_family") == "sans"
                else r"\rmfamily"
            )
            out.append(
                begin
                + "\n{"
                + family
                + rf"\color{{SourceAccent}}\fontsize{{{title_size:.2f}pt}}{{{title_size*1.12:.2f}pt}}\selectfont "
                + text
                + "}\n"
                + end
                + "\n\\vspace{0.20em}\n"
            )

        elif kind == "author":
            out.append(
                "\\begin{center}{\\sffamily\\small "
                + text
                + "}\\end{center}\\vspace{0.12em}\n"
            )

        elif kind == "affiliation":
            out.append(
                "\\noindent{\\sffamily\\fontsize{7.4pt}{8.8pt}\\selectfont "
                + text
                + "}\\par\n"
            )

        else:
            out.append(
                "\\noindent{\\sffamily\\scriptsize "
                + text
                + "}\\par\n"
            )

    current_columns = 1
    in_items = False

    def close_items() -> None:
        nonlocal in_items
        if in_items:
            out.append("\\end{itemize}\n")
            in_items = False

    def close_columns() -> None:
        nonlocal current_columns
        close_items()
        if current_columns > 1:
            out.append("\\end{multicols}\n")
        current_columns = 1

    def set_columns(desired: int) -> None:
        nonlocal current_columns
        desired = max(1, min(3, desired))
        if desired == current_columns:
            return

        close_items()

        if current_columns > 1:
            out.append("\\end{multicols}\n")

        current_columns = desired

        if current_columns > 1:
            out.append(
                rf"\begin{{multicols}}{{{current_columns}}}\raggedcolumns" + "\n"
            )

    def render_nonfloat_visual(block: dict) -> str:
        asset = Path(block["asset"]).relative_to(work_dir).as_posix()
        full = block.get("column") == "full"
        width = r"0.93\linewidth" if not full else r"0.90\textwidth"
        return (
            "\\begin{center}\n"
            rf"\includegraphics[width={width}]{{\detokenize{{{asset}}}}}"
            "\n\\end{center}\n"
        )

    for block in body_blocks:
        kind = block["kind"]

        if kind == "footer":
            continue

        desired_columns = max(
            1,
            min(3, int(block.get("flow_columns", style.get("columns", 1)))),
        )
        full_width = block.get("column") == "full" and desired_columns > 1

        if full_width:
            # A wide equation/figure/title-like block temporarily leaves the
            # local column flow. The next ordinary block reopens its own flow.
            close_columns()
        else:
            set_columns(desired_columns)

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
            out.append(render_nonfloat_visual(block))
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
            out.append(
                "\\noindent " + text + "\\par\\vspace{0.48em}\n"
            )

        elif kind == "section":
            out.append("\\SourceSection{" + text + "}\n")

        elif kind == "subsection":
            out.append("\\SourceSubsection{" + text + "}\n")

        elif kind == "caption":
            out.append(
                "\\noindent{\\small "
                + text
                + "}\\par\\vspace{0.32em}\n"
            )

        elif kind == "reference":
            out.append("\\noindent{\\small " + text + "}\\par\n")

        elif kind in {"author", "affiliation", "metadata"}:
            out.append("\\noindent{\\small " + text + "}\\par\n")

        else:
            out.append(text + "\\par\n")

    close_columns()
    out.append("\\end{document}\n")
    return "".join(out)


def compile_latex(
    tex_source: str,
    work_dir: Path,
    output_path: Path,
) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    tex_path = work_dir / "translated.tex"
    tex_source = _sanitize_unicode(tex_source)
    tex_path.write_text(tex_source, encoding="utf-8")

    engine = shutil.which("xelatex")
    if not engine:
        raise RuntimeError("xelatex was not found in the prepared runtime")

    final_stdout = ""

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
        final_stdout = proc.stdout

        if proc.returncode != 0:
            context = ""
            match = re.search(
                r"(?:^|\n)(?:\./)?translated\.tex:(\d+):",
                proc.stdout,
            )
            if match:
                line_no = int(match.group(1))
                source_lines = tex_source.splitlines()
                lo = max(0, line_no - 4)
                hi = min(len(source_lines), line_no + 3)
                context = (
                    "\n\nGenerated LaTeX near the failing line:\n"
                    + "\n".join(
                        f"{i + 1:04d}: {source_lines[i]}"
                        for i in range(lo, hi)
                    )
                )

            raise RuntimeError(
                f"XeLaTeX compilation failed on pass {run_no + 1}:\n"
                + proc.stdout[-12000:]
                + context
            )

    generated = work_dir / "translated.pdf"
    if not generated.exists():
        raise RuntimeError("XeLaTeX produced no translated.pdf")

    # Do not fail for ordinary line-adjustment warnings, but surface meaningful
    # overfull boxes so equation/layout regressions are visible in Actions.
    overfull = re.findall(
        r"Overfull \\hbox \(([\d.]+)pt too wide\)",
        final_stdout,
    )
    serious = [float(value) for value in overfull if float(value) >= 8.0]
    if serious:
        print(
            "Warning: XeLaTeX reported serious overfull boxes: "
            + ", ".join(f"{value:.1f}pt" for value in serious[:12]),
            flush=True,
        )

    shutil.copy2(generated, output_path)


def process_pdf(
    pdf_path: Path,
    target_language: str,
    work_dir: Path,
    output_path: Path,
    max_pages: int,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict:
    started = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)

    src_doc = pymupdf.open(pdf_path)
    try:
        source_pages = src_doc.page_count
    finally:
        src_doc.close()

    phase = time.perf_counter()
    style, strategy, blocks, translation_items = reconstruct_document(
        pdf_path,
        target_language,
        work_dir,
        max_pages=max_pages,
        progress_callback=progress_callback,
    )
    vision_seconds = time.perf_counter() - phase

    flow_counts = sorted(
        {
            int(block.get("flow_columns", 1))
            for block in blocks
            if block.get("kind") not in {"title", "author", "affiliation", "metadata"}
        }
    )
    print(
        f"Detected local column flows: {flow_counts}",
        flush=True,
    )

    if progress_callback:
        progress_callback(56, "수식 LaTeX 사전 검사를 실행하고 있습니다.")

    preflight_math_blocks(
        blocks,
        work_dir,
        pdf_path,
        progress_callback=progress_callback,
    )

    phase = time.perf_counter()
    print(
        f"Translation agent: translating {len(translation_items)} semantic text blocks; "
        "all inline/display math remains protected LaTeX",
        flush=True,
    )
    if progress_callback:
        progress_callback(
            58,
            f"수식 사전 검사 완료 · 전문용어 전략을 적용해 본문 번역을 시작합니다 · "
            f"{len(translation_items)}개 텍스트 블록",
        )

    def translation_progress(fraction: float, detail: str) -> None:
        if not progress_callback:
            return
        # Translation remains the longest visible interval: 58% -> 90%.
        percent = 58 + int(round(32 * max(0.0, min(1.0, fraction))))
        progress_callback(min(90, percent), detail)

    translations = translate_blocks(
        translation_items,
        target_language,
        strategy,
        progress_callback=translation_progress,
    )
    translation_seconds = time.perf_counter() - phase

    if progress_callback:
        progress_callback(91, "번역 완료 · LaTeX 문서를 조립하고 있습니다.")

    phase = time.perf_counter()
    tex_source = build_latex(
        style,
        blocks,
        translations,
        work_dir,
    )

    if progress_callback:
        progress_callback(93, "XeLaTeX로 최종 PDF를 조판하고 있습니다.")

    compile_latex(
        tex_source,
        work_dir,
        output_path,
    )
    latex_seconds = time.perf_counter() - phase

    if progress_callback:
        progress_callback(95, "PDF 조판 완료 · 결과 파일을 정리하고 있습니다.")

    out_doc = pymupdf.open(output_path)
    try:
        output_pages = out_doc.page_count
    finally:
        out_doc.close()

    if source_pages >= 3 and output_pages > max(
        source_pages * 2.4,
        source_pages + 12,
    ):
        raise RuntimeError(
            f"Style/content validation failed: {source_pages} source pages "
            f"became {output_pages} output pages, which is implausibly large."
        )

    total_seconds = time.perf_counter() - started
    print(
        "Timing summary: "
        f"vision+structure={vision_seconds:.1f}s, "
        f"translation={translation_seconds:.1f}s, "
        f"latex={latex_seconds:.1f}s, total={total_seconds:.1f}s",
        flush=True,
    )

    return {
        "pages": output_pages,
        "translated_segments": len(translation_items),
        "source_pages": source_pages,
        "columns": style.get("columns"),
        "column_flows": flow_counts,
        "field": strategy.get("field"),
        "subfield": strategy.get("subfield"),
        "render_mode": "vision-first-dynamic-columns-latex-v6.2",
    }
