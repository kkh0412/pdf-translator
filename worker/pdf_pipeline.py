from __future__ import annotations

import html
import re
from pathlib import Path

import pymupdf

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
        return any(ch in "+-*/=<>|_^" for ch in t)
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


def _style_from_font(font_name: str) -> tuple[str, str, str]:
    """Return CSS family, weight, style inferred from the source span font name."""
    f = (font_name or "").lower()
    serif_markers = ("times", "serif", "roman", "cmr", "minion", "palatino", "garamond")
    family = "serif" if any(x in f for x in serif_markers) else "sans-serif"
    weight = "700" if any(x in f for x in ("bold", "black", "demi", "semibold")) else "400"
    style = "italic" if any(x in f for x in ("italic", "oblique", "slanted")) else "normal"
    return family, weight, style


def extract_layout(pdf_path: Path, max_pages: int) -> tuple[list[dict], list[dict]]:
    """Extract only geometry/text metadata. No page rasterization is performed."""
    doc = pymupdf.open(pdf_path)
    try:
        if doc.page_count > max_pages:
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
            pages.append({"index": pno, "width": float(rect.width), "height": float(rect.height)})

            data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
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

                has_math = any(
                    _looks_like_math(span.get("text", ""), span.get("font", ""))
                    for span in block_spans
                )
                translatable = [
                    span
                    for span in block_spans
                    if _is_translatable(span.get("text", ""), span.get("font", ""))
                ]

                # Whole paragraph/heading blocks are best for translation context and reflow.
                if translatable and not has_math:
                    line_texts = []
                    for line in block.get("lines", []):
                        parts = [
                            span.get("text", "").strip()
                            for span in line.get("spans", [])
                            if span.get("text", "").strip()
                        ]
                        if parts:
                            line_texts.append(" ".join(parts))
                    text = " ".join(line_texts)
                    sizes = sorted(float(span.get("size", 9.0)) for span in translatable)
                    font_size = sizes[len(sizes) // 2]
                    sid = add_segment(
                        pno,
                        sid,
                        text,
                        block["bbox"],
                        font_size,
                        translatable[0].get("font", ""),
                    )
                    continue

                # Mixed math/text block: only replace contiguous language spans.
                for line in block.get("lines", []):
                    group = []
                    for span in line.get("spans", []):
                        if _is_translatable(span.get("text", ""), span.get("font", "")):
                            if group:
                                prev = group[-1]
                                gap = float(span["bbox"][0]) - float(prev["bbox"][2])
                                threshold = max(8.0, float(span.get("size", 9.0)) * 2.2)
                                if gap > threshold:
                                    sid = _flush_group(pno, sid, group, add_segment)
                                    group = []
                            group.append(span)
                        else:
                            if group:
                                sid = _flush_group(pno, sid, group, add_segment)
                                group = []
                    if group:
                        sid = _flush_group(pno, sid, group, add_segment)

        return pages, segments
    finally:
        doc.close()


def _flush_group(pno: int, sid: int, group: list[dict], add_segment) -> int:
    x0 = min(float(x["bbox"][0]) for x in group)
    y0 = min(float(x["bbox"][1]) for x in group)
    x1 = max(float(x["bbox"][2]) for x in group)
    y1 = max(float(x["bbox"][3]) for x in group)
    text = " ".join(x.get("text", "").strip() for x in group)
    sizes = sorted(float(x.get("size", 9.0)) for x in group)
    font_size = sizes[len(sizes) // 2]
    return add_segment(pno, sid, text, [x0, y0, x1, y1], font_size, group[0].get("font", ""))


def render_pdf(
    pdf_path: Path,
    output_path: Path,
    segments: list[dict],
    translations: dict[str, str],
) -> dict:
    """Overlay translations directly onto the original vector PDF using MuPDF.

    This avoids TeX Live / CJK font package installation entirely. The original page,
    figures, equations, and vector graphics stay untouched underneath the translated
    text boxes.
    """
    doc = pymupdf.open(pdf_path)
    scales: list[float] = []
    try:
        by_page: dict[int, list[dict]] = {}
        for seg in segments:
            by_page.setdefault(seg["page"], []).append(seg)

        for pno, page in enumerate(doc):
            for seg in by_page.get(pno, []):
                translated = translations.get(seg["id"], seg["text"]).strip()
                if not translated:
                    continue

                x0, y0, x1, y1 = seg["bbox"]
                bw = max(4.0, x1 - x0)
                bh = max(4.0, y1 - y0)

                # Small padding masks antialiasing from the original glyphs.
                pad_x = min(0.7, bw * 0.012)
                pad_y = min(0.5, bh * 0.035)
                mask = pymupdf.Rect(
                    max(0.0, x0 - pad_x),
                    max(0.0, y0 - pad_y),
                    min(page.rect.width, x1 + pad_x),
                    min(page.rect.height, y1 + pad_y),
                )

                # Preserve the previous demo's conservative behavior: visually cover
                # language glyphs without deleting nearby formula/vector objects.
                page.draw_rect(mask, color=None, fill=(1, 1, 1), overlay=True)

                font_size = max(5.0, min(float(seg["font_size"]), bh * 0.86))
                family, weight, style = _style_from_font(seg.get("font", ""))
                css = (
                    "* { margin: 0; padding: 0; } "
                    f"body {{ font-family: {family}; font-size: {font_size:.2f}pt; "
                    f"font-weight: {weight}; font-style: {style}; line-height: 1.05; "
                    "color: #000; }"
                )

                # insert_htmlbox uses HarfBuzz, supports CJK without an external font
                # package, and scales content down until it fits inside the rectangle.
                spare_height, scale = page.insert_htmlbox(
                    pymupdf.Rect(x0, y0, x1, y1),
                    html.escape(translated),
                    css=css,
                    scale_low=0.42,
                    overlay=True,
                )
                if spare_height < 0:
                    raise RuntimeError(
                        f"Translated text could not fit inside source box {seg['id']}. "
                        "Try a shorter translation or larger layout box."
                    )
                scales.append(float(scale))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(output_path, garbage=3, deflate=True)
    finally:
        doc.close()

    return {
        "min_text_scale": min(scales) if scales else 1.0,
        "avg_text_scale": (sum(scales) / len(scales)) if scales else 1.0,
    }


def process_pdf(
    pdf_path: Path,
    target_language: str,
    work_dir: Path,
    output_path: Path,
    max_pages: int,
) -> dict:
    # work_dir is kept in the function signature for compatibility with run_job.py.
    work_dir.mkdir(parents=True, exist_ok=True)
    pages, segments = extract_layout(pdf_path, max_pages=max_pages)
    if not segments:
        raise RuntimeError(
            "No selectable natural-language text was detected. This demo supports "
            "born-digital PDFs; scanned/image-only PDFs need OCR support."
        )

    translations = translate_segments(
        [{"id": s["id"], "text": s["text"]} for s in segments],
        target_language,
    )
    render_info = render_pdf(pdf_path, output_path, segments, translations)

    return {
        "pages": len(pages),
        "translated_segments": len(segments),
        **render_info,
    }
