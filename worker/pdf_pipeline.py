from __future__ import annotations

import math
import os
import re
import shutil
import statistics
import subprocess
import unicodedata
from pathlib import Path

import pymupdf

from .translator import translate_segments


MATH_FONT_MARKERS = (
    "math", "symbol", "cmsy", "cmmi", "cmex", "stix", "mtmi", "msam", "msbm",
)
BOLD_MARKERS = ("bold", "black", "demi", "semibold", "medium")
ITALIC_MARKERS = ("italic", "oblique", "slanted")
SMALLCAP_MARKERS = ("smallcaps", "small-caps", "smallcap", "largesmallcaps")
SERIF_MARKERS = (
    "kp-", "times", "serif", "roman", "cmr", "minion", "palatino", "pagella",
    "garamond", "libertinus", "baskerville", "bookman",
)


def _sanitize_unicode(text: str, keep_newlines: bool = False) -> str:
    """Normalize PDF/model text and remove control code points unsafe for XeTeX."""
    text = unicodedata.normalize("NFC", text or "")
    out: list[str] = []
    for ch in text:
        if ch == "\n" and keep_newlines:
            out.append(ch)
            continue
        if ch in {"\t", "\r", "\n"}:
            out.append(" ")
            continue
        # PDF extraction can contain NUL and other invisible control/private
        # characters. They are not document content and can make XeTeX abort.
        if unicodedata.category(ch).startswith("C"):
            continue
        # NFC composes common negated symbols (= + overlay -> ≠, ∈ + overlay -> ∉).
        # Drop a remaining standalone combining solidus overlay.
        if ch == "\u0338":
            continue
        out.append(ch)
    return "".join(out)


def _clean_text(text: str, keep_newlines: bool = False) -> str:
    text = _sanitize_unicode(text, keep_newlines=keep_newlines)
    if keep_newlines:
        lines = [" ".join(x.split()) for x in text.splitlines()]
        return "\n".join(x for x in lines if x)
    return " ".join(text.split())


def _looks_like_math(text: str, font: str) -> bool:
    t = text.strip()
    if not t:
        return True
    f = (font or "").lower()
    if any(marker in f for marker in MATH_FONT_MARKERS):
        return True
    if re.fullmatch(r"[\d\s.,;:()\[\]{}+\-*/=<>|_^%°′″]+", t):
        return any(ch in "+-*/=<>|_^" for ch in t)
    if len(t) <= 180 and ("=" in t or "^" in t or "_" in t):
        if re.search(r"[A-Za-zΑ-Ωα-ω]\s*(?:[=_^]|[+\-*/]\s*[A-Za-z0-9(])", t):
            return True
    letters = sum(ch.isalpha() for ch in t)
    mathish = sum(
        ch in "=<>±×÷∑∫√∞≈≃≤≥∂∇αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ"
        for ch in t
    )
    if letters == 0 and mathish > 0:
        return True
    if mathish >= 2 and mathish >= max(1, letters // 2):
        return True
    return False


def _is_page_number(text: str, bbox, page_height: float) -> bool:
    t = text.strip()
    if not re.fullmatch(r"\d{1,4}", t):
        return False
    return float(bbox[1]) > page_height * 0.86


def _weighted_median(values: list[tuple[float, int]], default: float) -> float:
    if not values:
        return default
    ordered = sorted(values)
    total = sum(max(1, w) for _, w in ordered)
    halfway = total / 2
    acc = 0
    for value, weight in ordered:
        acc += max(1, weight)
        if acc >= halfway:
            return value
    return ordered[-1][0]


def _font_style(spans: list[dict]) -> dict:
    if not spans:
        return {"font": "", "size": 10.0, "bold": 0.0, "italic": 0.0, "smallcaps": False}
    total = sum(max(1, len(s.get("text", "").strip())) for s in spans)
    font_weight: dict[str, int] = {}
    size_values: list[tuple[float, int]] = []
    bold = italic = 0
    smallcaps = False
    for s in spans:
        text = s.get("text", "").strip()
        w = max(1, len(text))
        font = s.get("font", "") or ""
        f = font.lower()
        font_weight[font] = font_weight.get(font, 0) + w
        size_values.append((float(s.get("size", 10.0)), w))
        if any(x in f for x in BOLD_MARKERS):
            bold += w
        if any(x in f for x in ITALIC_MARKERS):
            italic += w
        normalized_font = f.replace("-", "")
        if any(x.replace("-", "") in normalized_font for x in SMALLCAP_MARKERS):
            smallcaps = True
    dominant_font = max(font_weight, key=font_weight.get)
    return {
        "font": dominant_font,
        "size": _weighted_median(size_values, 10.0),
        "bold": bold / max(1, total),
        "italic": italic / max(1, total),
        "smallcaps": smallcaps,
    }


def _is_display_math(text: str, spans: list[dict]) -> bool:
    if not spans:
        return False
    total = sum(max(1, len(s.get("text", "").strip())) for s in spans)
    math_weight = sum(
        max(1, len(s.get("text", "").strip()))
        for s in spans
        if _looks_like_math(s.get("text", ""), s.get("font", ""))
    )
    natural_letters = sum(ch.isalpha() for ch in text)
    if math_weight / max(1, total) >= 0.55:
        return True
    if len(text) <= 180 and natural_letters <= 12 and re.search(r"[=∑∫√_^]", text):
        return True
    return False


def _classify_text_node(node: dict, profile: dict) -> str:
    text = node["text"].strip()
    font_size = node["font_size"]
    page_width = node["page_width"]
    page_height = node["page_height"]
    x0, y0, x1, _ = node["bbox"]
    width = x1 - x0
    center = (x0 + x1) / 2
    centered = abs(center - page_width / 2) < page_width * 0.075 and width < page_width * 0.82
    near_top = y0 < page_height * 0.18
    very_top = y0 < page_height * 0.10
    short = len(text) <= 120

    if very_top and short and (node["smallcaps"] or text.isupper() or text.lower() in {"contents", "content"}):
        return "header"
    if near_top and short and centered and (node["smallcaps"] or font_size >= profile["base_font_size"] * 1.08):
        return "title"
    if text.startswith(("—", "–", "-")) and node["italic_ratio"] > 0.45 and width < page_width * 0.55:
        return "attribution"
    if node["italic_ratio"] > 0.62 and len(text) > 150 and width < page_width * 0.72:
        return "verse"
    if re.match(r"^\s*\d+(?:\.\d+)*\.\s+", text) and short:
        # Bold numbered lines are list/topic headings even when their text box
        # happens to be geometrically centered. Centered italic numbered lines
        # are higher-level section headings.
        if node["bold_ratio"] > 0.18:
            return "topic"
        if centered or node["italic_ratio"] > 0.45:
            return "section"
    if re.match(r"^\s*[•▪◦‣●○]\s*", text):
        return "bullet"
    if font_size >= profile["base_font_size"] * 1.22 and short:
        return "title" if centered else "section"
    return "paragraph"


def _percentile(values: list[float], q: float, default: float) -> float:
    if not values:
        return default
    vals = sorted(values)
    idx = max(0, min(len(vals) - 1, round((len(vals) - 1) * q)))
    return vals[idx]


def _infer_profile(raw_text_nodes: list[dict], page_width: float, page_height: float) -> dict:
    size_values: list[tuple[float, int]] = []
    font_weights: dict[str, int] = {}
    lefts: list[float] = []
    rights: list[float] = []
    tops: list[float] = []
    bottoms: list[float] = []

    for node in raw_text_nodes:
        text = node["text"].strip()
        if not text or _is_page_number(text, node["bbox"], node["page_height"]):
            continue
        w = max(1, len(text))
        size_values.append((node["font_size"], w))
        font = node["font"]
        font_weights[font] = font_weights.get(font, 0) + w
        x0, y0, x1, y1 = node["bbox"]
        # Narrow centered blocks (titles / poems) should not define page margins.
        if not (abs((x0 + x1) / 2 - node["page_width"] / 2) < node["page_width"] * 0.08 and (x1 - x0) < node["page_width"] * 0.65):
            lefts.append(x0)
            rights.append(node["page_width"] - x1)
        tops.append(y0)
        bottoms.append(node["page_height"] - y1)

    base_size = _weighted_median(size_values, 10.5)
    dominant_font = max(font_weights, key=font_weights.get) if font_weights else ""
    serif = any(x in dominant_font.lower() for x in SERIF_MARKERS)

    left = _percentile(lefts, 0.08, 72.0)
    right = _percentile(rights, 0.08, 72.0)
    top = _percentile(tops, 0.08, 64.0)
    bottom = _percentile(bottoms, 0.08, 64.0)

    # Keep geometry book-like rather than allowing extreme OCR boxes to dominate.
    def clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    return {
        "base_font_size": clamp(base_size, 9.0, 12.5),
        "dominant_font": dominant_font,
        "serif": serif,
        "page_width": page_width,
        "page_height": page_height,
        "left_margin": clamp(left, 52.0, 100.0),
        "right_margin": clamp(right, 52.0, 100.0),
        "top_margin": clamp(top, 48.0, 82.0),
        "bottom_margin": clamp(bottom, 48.0, 82.0),
    }


def extract_document(pdf_path: Path, work_dir: Path, max_pages: int) -> tuple[dict, list[dict], list[dict]]:
    doc = pymupdf.open(pdf_path)
    assets_dir = work_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    try:
        if doc.page_count > max_pages:
            raise RuntimeError(f"This demo accepts at most {max_pages} pages per PDF")
        if doc.page_count == 0:
            raise RuntimeError("PDF has no pages")

        first = doc[0].rect
        raw_text_nodes: list[dict] = []
        nontext_nodes: list[dict] = []
        segment_counter = 0
        asset_counter = 0

        for pno, page in enumerate(doc):
            data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)
            for block in data.get("blocks", []):
                bbox = [float(v) for v in block.get("bbox", (0, 0, 0, 0))]
                if block.get("type") == 1:
                    x0, y0, x1, y1 = bbox
                    if x1 - x0 < 8 or y1 - y0 < 8:
                        continue
                    asset = assets_dir / f"asset_{asset_counter:04d}.png"
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(1.7, 1.7), clip=pymupdf.Rect(*bbox), alpha=False)
                    pix.save(asset)
                    nontext_nodes.append({
                        "kind": "figure",
                        "page": pno,
                        "bbox": bbox,
                        "asset": asset,
                        "width_ratio": min(1.0, max(0.18, (x1 - x0) / max(1.0, page.rect.width))),
                    })
                    asset_counter += 1
                    continue
                if block.get("type") != 0:
                    continue

                spans = [
                    span
                    for line in block.get("lines", [])
                    for span in line.get("spans", [])
                    if span.get("text", "").strip()
                ]
                if not spans:
                    continue
                line_texts = []
                for line in block.get("lines", []):
                    # Span boundaries frequently reflect font/ligature changes, not
                    # real word boundaries (e.g. Kp-Expert's ``ff`` ligature).
                    # Preserve the spaces already present in the PDF instead of
                    # inserting new ones between every span.
                    parts = [span.get("text", "") for span in line.get("spans", []) if span.get("text", "").strip()]
                    if parts:
                        line_texts.append("".join(parts).strip())

                # Undo soft hyphenation caused only by a PDF line break.
                merged_lines: list[str] = []
                for line_text in line_texts:
                    if (
                        merged_lines
                        and merged_lines[-1].endswith("-")
                        and line_text
                        and line_text[0].islower()
                    ):
                        merged_lines[-1] = merged_lines[-1][:-1] + line_text
                    else:
                        merged_lines.append(line_text)
                line_texts = merged_lines
                plain = _clean_text(" ".join(line_texts))
                if not plain:
                    continue
                style = _font_style(spans)
                raw_text_nodes.append({
                    "id": f"t{segment_counter}",
                    "page": pno,
                    "bbox": bbox,
                    "text": plain,
                    "line_text": "\n".join(line_texts),
                    "font": style["font"],
                    "font_size": style["size"],
                    "bold_ratio": style["bold"],
                    "italic_ratio": style["italic"],
                    "smallcaps": style["smallcaps"],
                    "page_width": float(page.rect.width),
                    "page_height": float(page.rect.height),
                    "spans": spans,
                })
                segment_counter += 1

        profile = _infer_profile(raw_text_nodes, float(first.width), float(first.height))

        # Preserve book-like mirrored margins when the source clearly uses them.
        # This is a style cue, not an attempt to pin every paragraph to an exact x/y.
        source_page_numbers: list[tuple[int, int]] = []
        page_edges: dict[int, tuple[float, float]] = {}
        for pno in range(doc.page_count):
            candidates = [
                n for n in raw_text_nodes
                if n["page"] == pno and not _is_page_number(n["text"], n["bbox"], n["page_height"])
            ]
            if candidates:
                left_edge = min(float(n["bbox"][0]) for n in candidates)
                right_edge = min(float(n["page_width"] - n["bbox"][2]) for n in candidates)
                page_edges[pno] = (left_edge, right_edge)

        for n in raw_text_nodes:
            if _is_page_number(n["text"], n["bbox"], n["page_height"]):
                try:
                    source_page_numbers.append((n["page"], int(n["text"].strip())))
                except ValueError:
                    pass
        source_page_numbers.sort()
        profile["start_page_number"] = source_page_numbers[0][1] if source_page_numbers else 1
        profile["twoside"] = False

        if 0 in page_edges and 1 in page_edges:
            l0, r0 = page_edges[0]
            l1, r1 = page_edges[1]
            mirrored = abs(l0 - r1) < 28.0 and abs(r0 - l1) < 28.0
            if mirrored:
                profile["twoside"] = True
                first_number = profile["start_page_number"]
                if first_number % 2 == 0:
                    profile["outer_margin"] = (l0 + r1) / 2
                    profile["inner_margin"] = (r0 + l1) / 2
                else:
                    profile["inner_margin"] = (l0 + r1) / 2
                    profile["outer_margin"] = (r0 + l1) / 2

        nodes: list[dict] = []
        translation_items: list[dict] = []

        for raw in raw_text_nodes:
            if _is_page_number(raw["text"], raw["bbox"], raw["page_height"]):
                continue
            if _is_display_math(raw["text"], raw["spans"]):
                page = doc[raw["page"]]
                x0, y0, x1, y1 = raw["bbox"]
                pad = 3.0
                rect = pymupdf.Rect(max(0, x0-pad), max(0, y0-pad), min(page.rect.width, x1+pad), min(page.rect.height, y1+pad))
                asset = assets_dir / f"asset_{asset_counter:04d}.png"
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0), clip=rect, alpha=False)
                pix.save(asset)
                nodes.append({
                    "kind": "equation",
                    "page": raw["page"],
                    "bbox": raw["bbox"],
                    "asset": asset,
                    "width_ratio": min(0.92, max(0.16, (x1-x0) / max(1.0, page.rect.width))),
                })
                asset_counter += 1
                continue

            kind = _classify_text_node(raw, profile)
            text = raw["line_text"] if kind == "verse" else raw["text"]
            text = _clean_text(text, keep_newlines=(kind == "verse"))
            if kind == "bullet":
                text = re.sub(r"^\s*[•▪◦‣●○]\s*", "", text)
            node = {
                "kind": kind,
                "id": raw["id"],
                "page": raw["page"],
                "bbox": raw["bbox"],
                "font_size": raw["font_size"],
                "bold_ratio": raw["bold_ratio"],
                "italic_ratio": raw["italic_ratio"],
                "smallcaps": raw["smallcaps"],
                "text": text,
            }
            nodes.append(node)
            translation_items.append({"id": raw["id"], "text": text, "kind": kind})

        nodes.extend(nontext_nodes)
        nodes.sort(key=lambda x: (x["page"], float(x["bbox"][1]), float(x["bbox"][0]), 0 if x["kind"] in {"figure", "equation"} else 1))
        return profile, nodes, translation_items
    finally:
        doc.close()


LATEX_REPLACEMENTS = {
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

# PDF text often contains Unicode math glyphs inside prose blocks. Kp text
# fonts intentionally do not cover every mathematical symbol. Convert those
# glyphs to real LaTeX math commands instead of relying on text-font coverage.
UNICODE_MATH_REPLACEMENTS = {
    "∈": r"\ensuremath{\in}",
    "∉": r"\ensuremath{\notin}",
    "∋": r"\ensuremath{\ni}",
    "∅": r"\ensuremath{\varnothing}",
    "∩": r"\ensuremath{\cap}",
    "∪": r"\ensuremath{\cup}",
    "⊂": r"\ensuremath{\subset}",
    "⊆": r"\ensuremath{\subseteq}",
    "⊃": r"\ensuremath{\supset}",
    "⊇": r"\ensuremath{\supseteq}",
    "≠": r"\ensuremath{\neq}",
    "≮": r"\ensuremath{\not<}",
    "≯": r"\ensuremath{\not>}",
    "≤": r"\ensuremath{\leq}",
    "≥": r"\ensuremath{\geq}",
    "≈": r"\ensuremath{\approx}",
    "≃": r"\ensuremath{\simeq}",
    "≅": r"\ensuremath{\cong}",
    "≡": r"\ensuremath{\equiv}",
    "∼": r"\ensuremath{\sim}",
    "∝": r"\ensuremath{\propto}",
    "±": r"\ensuremath{\pm}",
    "∓": r"\ensuremath{\mp}",
    "×": r"\ensuremath{\times}",
    "÷": r"\ensuremath{\div}",
    "·": r"\ensuremath{\cdot}",
    "⋅": r"\ensuremath{\cdot}",
    "∑": r"\ensuremath{\sum}",
    "∏": r"\ensuremath{\prod}",
    "∫": r"\ensuremath{\int}",
    "∮": r"\ensuremath{\oint}",
    "∞": r"\ensuremath{\infty}",
    "∂": r"\ensuremath{\partial}",
    "∇": r"\ensuremath{\nabla}",
    "∀": r"\ensuremath{\forall}",
    "∃": r"\ensuremath{\exists}",
    "¬": r"\ensuremath{\neg}",
    "∧": r"\ensuremath{\wedge}",
    "∨": r"\ensuremath{\vee}",
    "→": r"\ensuremath{\to}",
    "←": r"\ensuremath{\leftarrow}",
    "↔": r"\ensuremath{\leftrightarrow}",
    "⇒": r"\ensuremath{\Rightarrow}",
    "⇐": r"\ensuremath{\Leftarrow}",
    "⇔": r"\ensuremath{\Leftrightarrow}",
    "↦": r"\ensuremath{\mapsto}",
    "⊕": r"\ensuremath{\oplus}",
    "⊗": r"\ensuremath{\otimes}",
    "⊥": r"\ensuremath{\perp}",
    "∥": r"\ensuremath{\parallel}",
    "ℝ": r"\ensuremath{\mathbb{R}}",
    "ℂ": r"\ensuremath{\mathbb{C}}",
    "ℤ": r"\ensuremath{\mathbb{Z}}",
    "ℚ": r"\ensuremath{\mathbb{Q}}",
    "ℕ": r"\ensuremath{\mathbb{N}}",
    "α": r"\ensuremath{\alpha}",
    "β": r"\ensuremath{\beta}",
    "γ": r"\ensuremath{\gamma}",
    "δ": r"\ensuremath{\delta}",
    "ε": r"\ensuremath{\epsilon}",
    "ζ": r"\ensuremath{\zeta}",
    "η": r"\ensuremath{\eta}",
    "θ": r"\ensuremath{\theta}",
    "ι": r"\ensuremath{\iota}",
    "κ": r"\ensuremath{\kappa}",
    "λ": r"\ensuremath{\lambda}",
    "μ": r"\ensuremath{\mu}",
    "ν": r"\ensuremath{\nu}",
    "ξ": r"\ensuremath{\xi}",
    "π": r"\ensuremath{\pi}",
    "ρ": r"\ensuremath{\rho}",
    "σ": r"\ensuremath{\sigma}",
    "τ": r"\ensuremath{\tau}",
    "φ": r"\ensuremath{\phi}",
    "χ": r"\ensuremath{\chi}",
    "ψ": r"\ensuremath{\psi}",
    "ω": r"\ensuremath{\omega}",
    "Γ": r"\ensuremath{\Gamma}",
    "Δ": r"\ensuremath{\Delta}",
    "Θ": r"\ensuremath{\Theta}",
    "Λ": r"\ensuremath{\Lambda}",
    "Ξ": r"\ensuremath{\Xi}",
    "Π": r"\ensuremath{\Pi}",
    "Σ": r"\ensuremath{\Sigma}",
    "Φ": r"\ensuremath{\Phi}",
    "Ψ": r"\ensuremath{\Psi}",
    "Ω": r"\ensuremath{\Omega}",
}


def _latex_escape(text: str) -> str:
    text = _sanitize_unicode(text, keep_newlines=True)
    out: list[str] = []
    for ch in text:
        math_replacement = UNICODE_MATH_REPLACEMENTS.get(ch)
        if math_replacement is not None:
            out.append(math_replacement)
        else:
            out.append(LATEX_REPLACEMENTS.get(ch, ch))
    return "".join(out)


def _latex_text(text: str, preserve_lines: bool = False) -> str:
    if preserve_lines:
        return r" \\ ".join(_latex_escape(line) for line in text.splitlines())
    return _latex_escape(text)


def _pt(v: float) -> str:
    return f"{v:.2f}pt"


def _font_setup(profile: dict, font_dir: Path) -> str:
    """Use Kp OpenType font files directly instead of kpfonts-otf.sty.

    This keeps the source document's Kp-style typography while avoiding
    kpfonts-otf.sty's package dependencies (realscripts / unicode-math).
    """
    if profile.get("serif", True):
        latin = r"""
\setmainfont[
  Path={font/},
  UprightFont=KpRoman-Regular.otf,
  ItalicFont=KpRoman-Italic.otf,
  BoldFont=KpRoman-Bold.otf,
  BoldItalicFont=KpRoman-BoldItalic.otf
]{KpRoman-Regular.otf}
"""
    else:
        latin = r"""
\setmainfont[
  Path={font/},
  UprightFont=KpSans-Regular.otf,
  ItalicFont=KpSans-Italic.otf,
  BoldFont=KpSans-Bold.otf,
  BoldItalicFont=KpSans-BoldItalic.otf
]{KpSans-Regular.otf}
"""
    return latin + r"""
\setmainhangulfont[
  Path={font/},
  UprightFont=NanumMyeongjo-Regular.ttf,
  BoldFont=NanumMyeongjo-Bold.ttf,
  ItalicFont=NanumMyeongjo-Regular.ttf,
  ItalicFeatures={FakeSlant=0.13},
  BoldItalicFont=NanumMyeongjo-Bold.ttf,
  BoldItalicFeatures={FakeSlant=0.13}
]{NanumMyeongjo-Regular.ttf}
"""

def _latex_topic(text: str) -> str:
    """Keep topic numbering and trailing lecture count light, with only the title bold."""
    match = re.match(r"^(\d+(?:\.\d+)*\.\s*)(.*?)(\s*\([^)]*\))$", text.strip())
    if match:
        prefix, title, suffix = match.groups()
        return _latex_escape(prefix) + r"\textbf{" + _latex_escape(title.strip()) + "}" + _latex_escape(suffix)

    # If translation changed the parenthesis style, at least keep the numeric prefix light.
    match = re.match(r"^(\d+(?:\.\d+)*\.\s*)(.*)$", text.strip())
    if match:
        prefix, title = match.groups()
        return _latex_escape(prefix) + r"\textbf{" + _latex_escape(title.strip()) + "}"
    return r"\textbf{" + _latex_escape(text.strip()) + "}"


def build_latex(profile: dict, nodes: list[dict], translations: dict[str, str], work_dir: Path) -> str:
    source_font_dir = Path(
        os.getenv("BOOK_FONT_DIR", os.getenv("KOREAN_FONT_DIR", "/usr/share/fonts/truetype/nanum"))
    )

    korean_regular_candidates = [
        source_font_dir / "NanumMyeongjo-Regular.ttf",
        source_font_dir / "NanumMyeongjo.ttf",
        Path("/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf"),
    ]
    korean_bold_candidates = [
        source_font_dir / "NanumMyeongjo-Bold.ttf",
        source_font_dir / "NanumMyeongjoBold.ttf",
        Path("/usr/share/fonts/truetype/nanum/NanumMyeongjoBold.ttf"),
    ]

    def first_existing(paths: list[Path]) -> Path | None:
        return next((x for x in paths if x.exists()), None)

    korean_regular = first_existing(korean_regular_candidates)
    korean_bold = first_existing(korean_bold_candidates)
    if not korean_regular or not korean_bold:
        raise RuntimeError(
            f"Korean book font files were not found in {source_font_dir}. "
            "Expected NanumMyeongjo regular and bold font files."
        )

    if profile.get("serif", True):
        latin_names = [
            "KpRoman-Regular.otf",
            "KpRoman-Italic.otf",
            "KpRoman-Bold.otf",
            "KpRoman-BoldItalic.otf",
        ]
    else:
        latin_names = [
            "KpSans-Regular.otf",
            "KpSans-Italic.otf",
            "KpSans-Bold.otf",
            "KpSans-BoldItalic.otf",
        ]

    missing_latin = [name for name in latin_names if not (source_font_dir / name).exists()]
    if missing_latin:
        raise RuntimeError(
            "Book Latin font files are missing from "
            f"{source_font_dir}: {', '.join(missing_latin)}"
        )

    font_dir = work_dir / "font"
    font_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(korean_regular, font_dir / "NanumMyeongjo-Regular.ttf")
    shutil.copy2(korean_bold, font_dir / "NanumMyeongjo-Bold.ttf")
    for name in latin_names:
        shutil.copy2(source_font_dir / name, font_dir / name)

    base = profile["base_font_size"]
    class_options = "10pt,twoside" if profile.get("twoside") else "10pt"
    if profile.get("twoside"):
        geometry_lines = (
            f"paperwidth={_pt(profile['page_width'])},\n"
            f"  paperheight={_pt(profile['page_height'])},\n"
            f"  inner={_pt(profile.get('inner_margin', profile['left_margin']))},\n"
            f"  outer={_pt(profile.get('outer_margin', profile['right_margin']))},\n"
            f"  top={_pt(profile['top_margin'])},\n"
            f"  bottom={_pt(profile['bottom_margin'])}"
        )
    else:
        geometry_lines = (
            f"paperwidth={_pt(profile['page_width'])},\n"
            f"  paperheight={_pt(profile['page_height'])},\n"
            f"  left={_pt(profile['left_margin'])},\n"
            f"  right={_pt(profile['right_margin'])},\n"
            f"  top={_pt(profile['top_margin'])},\n"
            f"  bottom={_pt(profile['bottom_margin'])}"
        )

    latin_setup = _font_setup(profile, font_dir)

    preamble = rf"""\documentclass[{class_options}]{{article}}
\usepackage{{fontspec}}
\usepackage{{xetexko}}
\usepackage{{geometry}}
\usepackage{{graphicx}}
\usepackage{{enumitem}}
\usepackage{{amsmath,amssymb}}
\geometry{{
  {geometry_lines}
}}
{latin_setup}
\AtBeginDocument{{\fontsize{{{base:.2f}pt}}{{{base*1.27:.2f}pt}}\selectfont}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{0.43em}}
\setlength{{\emergencystretch}}{{2em}}
\linespread{{1.07}}
\setlist[itemize]{{leftmargin=2.1em,itemsep=0.24em,topsep=0.28em,parsep=0pt}}
\raggedbottom
\makeatletter
\def\ps@bookish{{%
  \def\@oddhead{{}}\def\@evenhead{{}}%
  \def\@oddfoot{{\hfill\thepage}}%
  \def\@evenfoot{{\thepage\hfill}}%
}}
\makeatother
\pagestyle{{bookish}}
\setcounter{{page}}{{{int(profile.get('start_page_number', 1))}}}
\newcommand{{\DocHeader}}[1]{{\noindent{{\small\scshape #1}}\par\vspace{{2pt}}\hrule\vspace{{1.45em}}}}
\newcommand{{\DocTitle}}[1]{{\begin{{center}}\large\scshape #1\end{{center}}\vspace{{0.45em}}}}
\newcommand{{\DocSection}}[1]{{\vspace{{0.85em}}\begin{{center}}\large\itshape #1\end{{center}}\vspace{{0.20em}}}}
\newcommand{{\DocTopic}}[1]{{\par\vspace{{0.42em}}\noindent #1\par\vspace{{0.08em}}}}
\begin{{document}}
"""

    out: list[str] = [preamble]
    in_items = False

    def close_items() -> None:
        nonlocal in_items
        if in_items:
            out.append("\\end{itemize}\n")
            in_items = False

    for node in nodes:
        kind = node["kind"]
        if kind == "bullet":
            if not in_items:
                out.append("\\begin{itemize}\n")
                in_items = True
            text = translations.get(node["id"], node["text"]).strip()
            out.append(f"\\item {_latex_text(text)}\n")
            continue

        close_items()

        if kind in {"figure", "equation"}:
            rel = Path(node["asset"]).relative_to(work_dir).as_posix()
            width = min(0.96, max(0.22, float(node.get("width_ratio", 0.7))))
            if kind == "equation":
                out.append(
                    f"\\begin{{center}}\\includegraphics[width={width:.2f}\\linewidth]{{\\detokenize{{{rel}}}}}\\end{{center}}\n"
                )
            else:
                out.append(
                    f"\\begin{{center}}\\includegraphics[width={width:.2f}\\linewidth]{{\\detokenize{{{rel}}}}}\\end{{center}}\n"
                )
            continue

        text = translations.get(node["id"], node["text"]).strip()
        if not text:
            continue
        escaped = _latex_text(text, preserve_lines=(kind == "verse"))

        if kind == "header":
            out.append(f"\\DocHeader{{{escaped}}}\n")
        elif kind == "title":
            out.append(f"\\DocTitle{{{escaped}}}\n")
        elif kind == "section":
            out.append(f"\\DocSection{{{escaped}}}\n")
        elif kind == "topic":
            out.append(f"\\DocTopic{{{_latex_topic(text)}}}\n")
        elif kind == "verse":
            out.append(
                "\\begin{center}\\begin{minipage}{0.62\\linewidth}"
                "\\itshape\\small " + escaped +
                "\\end{minipage}\\end{center}\\vspace{0.25em}\n"
            )
        elif kind == "attribution":
            out.append(f"\\begin{{flushright}}\\itshape\\small {escaped}\\end{{flushright}}\\vspace{{0.35em}}\n")
        else:
            prefix = "\\textbf{" if node.get("bold_ratio", 0.0) > 0.55 else ""
            suffix = "}" if prefix else ""
            if node.get("italic_ratio", 0.0) > 0.60:
                out.append(f"\\textit{{{escaped}}}\n\n")
            else:
                out.append(f"{prefix}{escaped}{suffix}\n\n")

    close_items()
    out.append("\\end{document}\n")
    return "".join(out)


def compile_latex(tex_source: str, work_dir: Path, output_path: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    tex_path = work_dir / "translated.tex"
    # Final boundary check: never pass hidden PDF/OCR control bytes to XeTeX.
    tex_source = _sanitize_unicode(tex_source, keep_newlines=True)
    if "\x00" in tex_source:
        raise RuntimeError("Internal error: NUL remained in generated LaTeX")
    tex_path.write_text(tex_source, encoding="utf-8")

    engine = shutil.which("xelatex")
    if not engine:
        raise RuntimeError(
            "xelatex was not found. The GitHub worker must restore/install the cached TinyTeX environment."
        )

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
        timeout=180,
    )
    if proc.returncode != 0:
        log = proc.stdout[-12000:]
        raise RuntimeError(f"XeLaTeX compilation failed:\n{log}")

    generated = work_dir / "translated.pdf"
    if not generated.exists():
        raise RuntimeError("XeLaTeX finished without producing translated.pdf")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(generated, output_path)


def process_pdf(
    pdf_path: Path,
    target_language: str,
    work_dir: Path,
    output_path: Path,
    max_pages: int,
) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    profile, nodes, translation_items = extract_document(pdf_path, work_dir, max_pages=max_pages)
    if not translation_items and not any(n["kind"] in {"figure", "equation"} for n in nodes):
        raise RuntimeError(
            "No selectable document content was detected. This demo currently supports born-digital PDFs; "
            "scanned/image-only PDFs need OCR support."
        )

    translations = translate_segments(translation_items, target_language)
    tex_source = build_latex(profile, nodes, translations, work_dir)
    compile_latex(tex_source, work_dir, output_path)

    out_doc = pymupdf.open(output_path)
    try:
        output_pages = out_doc.page_count
    finally:
        out_doc.close()

    return {
        "pages": output_pages,
        "translated_segments": len(translation_items),
        "source_font": profile["dominant_font"],
        "render_mode": "semantic-latex",
    }
