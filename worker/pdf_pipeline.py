from __future__ import annotations

import html
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import fitz

from .translator import translate_segments


MATH_FONT_MARKERS = (
    "math", "symbol", "cmsy", "cmmi", "cmex", "stix", "mtmi", "msam", "msbm",
)


def _looks_like_math(text: str, font: str) -> bool:
    t = text.strip()
    if not t:
        return True
    f = font.lower()
    if any(marker in f for marker in MATH_FONT_MARKERS):
        return True
    if re.fullmatch(r"[\d\s.,;:()\[\]{}+\-*/=<>|_^%°′″]+", t):
        # A citation/page number such as "[12]" is not math by itself. Operators make it math-like.
        return any(ch in "+-*/=<>|_^" for ch in t)
    # Be deliberately conservative around equation-like spans. Preserving an occasional
    # short phrase is safer than painting over a formula and translating its symbols.
    if len(t) <= 120 and ("=" in t or "^" in t or "_" in t):
        if re.search(r"[A-Za-zΑ-Ωα-ω]\s*(?:[=_^]|[+\-*/]\s*[A-Za-z0-9(])", t):
            return True
    letters = sum(ch.isalpha() for ch in t)
    mathish = sum(ch in "=<>±×÷∑∫√∞≈≃≤≥∂∇αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ" for ch in t)
    if letters == 0 and mathish > 0:
        return True
    if mathish >= 2 and mathish >= max(1, letters // 2):
        return True
    return False


def _is_translatable(text: str, font: str) -> bool:
    t = " ".join(text.split())
    if len(t) < 2:
        return False
    if _looks_like_math(t, font):
        return False
    if re.fullmatch(r"https?://\S+|www\.\S+|\S+@\S+", t):
        return False
    return any(ch.isalpha() for ch in t)


def _latex_escape(text: str) -> str:
    # Render all translated content as text. Math remains in the untouched page background.
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "%": r"\%",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "_": r"\_",
        "^": r"\textasciicircum{}",
        "~": r"\textasciitilde{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def extract_layout(pdf_path: Path, work_dir: Path, max_pages: int) -> tuple[list[dict], list[dict]]:
    doc = fitz.open(pdf_path)
    if doc.page_count > max_pages:
        doc.close()
        raise RuntimeError(f"This demo accepts at most {max_pages} pages per PDF")

    pages: list[dict] = []
    segments: list[dict] = []

    def add_segment(pno: int, sid: int, text: str, bbox, font_size: float, font: str) -> int:
        text = " ".join(text.split())
        if not text:
            return sid
        x0, y0, x1, y1 = map(float, bbox)
        if x1 - x0 < 3 or y1 - y0 < 3:
            return sid
        segments.append(
            {
                "id": f"p{pno}_s{sid}",
                "page": pno,
                "text": text,
                "bbox": [x0, y0, x1, y1],
                "font_size": float(font_size or 9.0),
                "font": font,
            }
        )
        return sid + 1

    for pno, page in enumerate(doc):
        rect = page.rect
        # 2x rasterization: figures/equations remain visually intact beneath translated text.
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        img_name = f"page_{pno + 1:04d}.png"
        pix.save(work_dir / img_name)

        pages.append(
            {
                "index": pno,
                "width": float(rect.width),
                "height": float(rect.height),
                "image": img_name,
            }
        )

        data = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
        sid = 0
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue

            block_spans = [
                span
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if span.get("text", "").strip()
            ]
            if not block_spans:
                continue

            has_math = any(_looks_like_math(span.get("text", ""), span.get("font", "")) for span in block_spans)
            translatable = [span for span in block_spans if _is_translatable(span.get("text", ""), span.get("font", ""))]

            # Preferred path: translate a whole paragraph/heading block at once. This keeps context and
            # lets TeX reflow the translation inside the original block rectangle.
            if translatable and not has_math:
                line_texts = []
                for line in block.get("lines", []):
                    parts = [span.get("text", "").strip() for span in line.get("spans", []) if span.get("text", "").strip()]
                    if parts:
                        line_texts.append(" ".join(parts))
                text = " ".join(line_texts)
                sizes = sorted(float(span.get("size", 9.0)) for span in translatable)
                font_size = sizes[len(sizes) // 2]
                sid = add_segment(pno, sid, text, block["bbox"], font_size, translatable[0].get("font", ""))
                continue

            # Conservative fallback for blocks containing math: only replace contiguous natural-language
            # spans and leave mathematical spans untouched in the page background.
            for line in block.get("lines", []):
                group = []
                for span in line.get("spans", []):
                    if _is_translatable(span.get("text", ""), span.get("font", "")):
                        if group:
                            prev = group[-1]
                            gap = float(span["bbox"][0]) - float(prev["bbox"][2])
                            threshold = max(8.0, float(span.get("size", 9.0)) * 2.2)
                            if gap > threshold:
                                x0 = min(float(x["bbox"][0]) for x in group)
                                y0 = min(float(x["bbox"][1]) for x in group)
                                x1 = max(float(x["bbox"][2]) for x in group)
                                y1 = max(float(x["bbox"][3]) for x in group)
                                text = " ".join(x.get("text", "").strip() for x in group)
                                sid = add_segment(pno, sid, text, [x0, y0, x1, y1], float(group[0].get("size", 9.0)), group[0].get("font", ""))
                                group = []
                        group.append(span)
                    else:
                        if group:
                            x0 = min(float(x["bbox"][0]) for x in group)
                            y0 = min(float(x["bbox"][1]) for x in group)
                            x1 = max(float(x["bbox"][2]) for x in group)
                            y1 = max(float(x["bbox"][3]) for x in group)
                            text = " ".join(x.get("text", "").strip() for x in group)
                            sid = add_segment(pno, sid, text, [x0, y0, x1, y1], float(group[0].get("size", 9.0)), group[0].get("font", ""))
                            group = []
                if group:
                    x0 = min(float(x["bbox"][0]) for x in group)
                    y0 = min(float(x["bbox"][1]) for x in group)
                    x1 = max(float(x["bbox"][2]) for x in group)
                    y1 = max(float(x["bbox"][3]) for x in group)
                    text = " ".join(x.get("text", "").strip() for x in group)
                    sid = add_segment(pno, sid, text, [x0, y0, x1, y1], float(group[0].get("size", 9.0)), group[0].get("font", ""))

    doc.close()
    return pages, segments

def build_tex(pages: list[dict], segments: list[dict], translations: dict[str, str], work_dir: Path) -> Path:
    by_page: dict[int, list[dict]] = {}
    for seg in segments:
        by_page.setdefault(seg["page"], []).append(seg)

    first_w = pages[0]["width"]
    first_h = pages[0]["height"]
    parts = [
        r"\documentclass{article}",
        rf"\usepackage[paperwidth={first_w:.3f}bp,paperheight={first_h:.3f}bp,margin=0pt]{{geometry}}",
        r"\usepackage{graphicx}",
        r"\usepackage{tikz}",
        r"\usepackage{adjustbox}",
        r"\usepackage{fontspec}",
        r"\usepackage{xeCJK}",
        r"\usepackage[absolute,overlay]{textpos}",
        r"\setmainfont{Noto Sans}",
        r"\setCJKmainfont{Noto Sans CJK KR}",
        r"\pagestyle{empty}",
        r"\setlength{\parindent}{0pt}",
        r"\begin{document}",
    ]

    for page in pages:
        w = page["width"]
        h = page["height"]
        parts.extend(
            [
                r"\thispagestyle{empty}",
                rf"\begin{{tikzpicture}}[remember picture,overlay]",
                rf"\node[anchor=north west,inner sep=0pt] at (current page.north west) "
                rf"{{\includegraphics[width={w:.3f}bp,height={h:.3f}bp]{{{page['image']}}}}};",
            ]
        )

        for seg in by_page.get(page["index"], []):
            translated = translations.get(seg["id"], seg["text"])
            if not translated:
                continue
            x0, y0, x1, y1 = seg["bbox"]
            bw = max(4.0, x1 - x0)
            bh = max(4.0, y1 - y0)
            # Small padding masks antialiasing from the original glyphs without invading nearby formula spans too much.
            pad_x = min(0.8, bw * 0.015)
            pad_y = min(0.6, bh * 0.04)
            rx0 = max(0.0, x0 - pad_x)
            ry0 = max(0.0, y0 - pad_y)
            rw = min(w - rx0, bw + 2 * pad_x)
            rh = min(h - ry0, bh + 2 * pad_y)
            fs = max(5.2, min(seg["font_size"], bh * 0.82))
            leading = fs * 1.08
            safe = _latex_escape(translated)

            parts.append(
                rf"\fill[white] ([xshift={rx0:.3f}bp,yshift=-{ry0:.3f}bp]current page.north west) "
                rf"rectangle ++({rw:.3f}bp,-{rh:.3f}bp);"
            )
            parts.append(
                rf"\node[anchor=north west,inner sep=0pt] at "
                rf"([xshift={x0:.3f}bp,yshift=-{y0:.3f}bp]current page.north west) "
                rf"{{\begin{{adjustbox}}{{max width={bw:.3f}bp,max totalheight={bh:.3f}bp}}"
                rf"\begin{{minipage}}[t]{{{bw:.3f}bp}}\raggedright\sloppy"
                rf"\fontsize{{{fs:.3f}bp}}{{{leading:.3f}bp}}\selectfont {safe}"
                rf"\end{{minipage}}\end{{adjustbox}}}};"
            )

        parts.extend([r"\end{tikzpicture}", r"\null", r"\newpage"])

    parts.append(r"\end{document}")
    tex_path = work_dir / "translated.tex"
    tex_path.write_text("\n".join(parts), encoding="utf-8")
    return tex_path


def compile_tex(tex_path: Path, work_dir: Path, output_path: Path) -> None:
    cmd = [
        "xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-directory",
        str(work_dir.resolve()),
        str(tex_path.resolve()),
    ]
    logs = []
    proc = None
    # TikZ current-page anchors need a second pass to settle page coordinates.
    for _ in range(2):
        proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, timeout=180)
        logs.append(proc.stdout + "\n" + proc.stderr)
        if proc.returncode != 0:
            break
    generated = work_dir / "translated.pdf"
    if proc is None or proc.returncode != 0 or not generated.exists():
        log_tail = "\n".join(logs)[-5000:]
        raise RuntimeError("XeLaTeX compilation failed:\n" + log_tail)
    shutil.copy2(generated, output_path)


def process_pdf(pdf_path: Path, target_language: str, work_dir: Path, output_path: Path, max_pages: int) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    pages, segments = extract_layout(pdf_path, work_dir, max_pages=max_pages)
    if not segments:
        raise RuntimeError(
            "No selectable natural-language text was detected. This first demo supports born-digital PDFs; scanned/image-only PDFs need OCR support."
        )
    translations = translate_segments(
        [{"id": s["id"], "text": s["text"]} for s in segments],
        target_language,
    )
    tex_path = build_tex(pages, segments, translations, work_dir)
    compile_tex(tex_path, work_dir, output_path)
    return {
        "pages": len(pages),
        "translated_segments": len(segments),
        "tex_path": str(tex_path),
    }
