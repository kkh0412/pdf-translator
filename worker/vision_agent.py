from __future__ import annotations

import base64
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

import pymupdf

from .gemini_rate import impose_cooldown, retry_delay_from_text, wait_for_slot


LANGUAGE_NAMES = {
    "ko": "Korean",
    "en": "English",
    "ja": "Japanese",
    "zh-CN": "Simplified Chinese",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
}


class GeminiVisionError(RuntimeError):
    pass


def _model_candidates() -> list[str]:
    primary = os.getenv("GEMINI_VISION_MODEL", "gemini-3.5-flash-lite").strip()
    models = [
        primary,
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
    ]
    out: list[str] = []
    for model in models:
        if model and model not in out:
            out.append(model)
    return out


def _response_text(response: dict) -> str:
    candidates = response.get("candidates") or []
    if not candidates:
        raise GeminiVisionError(f"Gemini returned no candidates: {response}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if part.get("text"))
    if not text:
        raise GeminiVisionError("Gemini returned no text output")
    return text


def _generation_config(schema: dict, mode: str) -> dict:
    base = {"thinkingConfig": {"thinkingLevel": "low"}}

    if mode == "enum_response_format":
        base["responseFormat"] = {
            "text": {
                "mimeType": "APPLICATION_JSON",
                "schema": schema,
            }
        }
        return base

    if mode == "legacy_json_schema":
        base["responseMimeType"] = "application/json"
        base["responseJsonSchema"] = schema
        return base

    raise ValueError(f"Unknown structured-output mode: {mode}")


def _is_schema_format_400(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "response_format",
            "responseformat",
            "mime_type",
            "mimetype",
            "responsemimetype",
            "responsejsonschema",
            "invalid_argument",
        )
    )


def _call_json(
    prompt: str,
    images: list[tuple[bytes, str]],
    schema: dict,
    label: str,
) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    last_error: Exception | None = None

    for model in _model_candidates():
        model_unavailable = False

        for format_mode in ("enum_response_format", "legacy_json_schema"):
            for attempt in range(2):
                parts = [{"text": prompt}]
                for image_bytes, mime in images:
                    parts.append(
                        {
                            "inlineData": {
                                "mimeType": mime,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        }
                    )

                body = {
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": _generation_config(schema, format_mode),
                }
                endpoint = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent"
                )
                request = urllib.request.Request(
                    endpoint,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": api_key,
                    },
                )

                try:
                    print(
                        f"Vision agent {label}: model={model}, "
                        f"format={format_mode}, attempt={attempt + 1}",
                        flush=True,
                    )
                    wait_for_slot(model)
                    with urllib.request.urlopen(request, timeout=180) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    return json.loads(_response_text(payload))

                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    last_error = RuntimeError(
                        f"Gemini vision HTTP {exc.code}: {detail}"
                    )

                    if exc.code == 400 and _is_schema_format_400(detail):
                        print(
                            f"Vision structured-output format rejected "
                            f"({format_mode}); trying compatibility format.",
                            flush=True,
                        )
                        break

                    if exc.code == 404:
                        model_unavailable = True
                        break

                    if exc.code == 429:
                        wait_seconds = retry_delay_from_text(
                            detail,
                            default=60.0,
                        ) + 1.0
                        impose_cooldown(model, wait_seconds)
                        print(
                            f"Vision Gemini 429 for {model}: shared cooldown "
                            f"{wait_seconds:.1f}s; retrying the same model.",
                            flush=True,
                        )
                        if attempt == 0:
                            continue
                        raise GeminiVisionError(
                            f"Vision rate limited after Retry-After wait: {detail}"
                        )

                    if exc.code in {500, 502, 503, 504}:
                        if attempt == 0:
                            time.sleep(2)
                            continue
                        break

                    raise last_error

                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    break

            if model_unavailable:
                break

            if isinstance(last_error, RuntimeError):
                message = str(last_error)
                if "HTTP 400" in message and _is_schema_format_400(message):
                    continue

        if model_unavailable:
            continue

    raise GeminiVisionError(f"Vision agent failed for {label}: {last_error}")


def _render_page(page: pymupdf.Page, dpi: int = 128) -> bytes:
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return pix.tobytes("jpeg", jpg_quality=76)


def _block_text(block: dict) -> str:
    lines: list[str] = []
    for line in block.get("lines", []):
        text = "".join(span.get("text", "") for span in line.get("spans", []))
        text = text.strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _font_name_is_bold(font: str) -> bool:
    font = str(font or "").lower()
    return any(
        token in font
        for token in ("bold", "semibold", "demibold", "heavy", "black")
    )


def _span_is_bold(span: dict) -> bool:
    flags = int(span.get("flags", 0) or 0)
    return bool(flags & 16) or _font_name_is_bold(span.get("font", ""))


def _span_is_italic(span: dict) -> bool:
    flags = int(span.get("flags", 0) or 0)
    font = str(span.get("font", "") or "").lower()
    return bool(flags & 2) or any(
        token in font for token in ("italic", "oblique", "slanted")
    )


def _page_hints(page: pymupdf.Page) -> list[dict]:
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)
    width = max(1.0, float(page.rect.width))
    height = max(1.0, float(page.rect.height))
    hints: list[dict] = []

    for n, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        text = _block_text(block).strip()
        if not text:
            continue

        x0, y0, x1, y1 = [float(v) for v in block["bbox"]]
        spans = [
            span
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if span.get("text", "").strip()
        ]

        fonts: dict[str, int] = {}
        sizes: list[float] = []
        for span in spans:
            font = span.get("font", "") or ""
            fonts[font] = fonts.get(font, 0) + max(1, len(span.get("text", "")))
            sizes.append(float(span.get("size", 10.0)))

        weighted_chars = sum(
            max(1, len(span.get("text", "")))
            for span in spans
        )
        bold_chars = sum(
            max(1, len(span.get("text", "")))
            for span in spans
            if _span_is_bold(span)
        )
        italic_chars = sum(
            max(1, len(span.get("text", "")))
            for span in spans
            if _span_is_italic(span)
        )

        hints.append(
            {
                "id": f"b{n}",
                "bbox": [
                    round(1000 * x0 / width),
                    round(1000 * y0 / height),
                    round(1000 * x1 / width),
                    round(1000 * y1 / height),
                ],
                "text": text[:5000],
                "font": max(fonts, key=fonts.get) if fonts else "",
                "font_size": round(statistics.median(sizes), 2) if sizes else 10.0,
                "bold_ratio": round(bold_chars / max(1, weighted_chars), 3),
                "italic_ratio": round(italic_chars / max(1, weighted_chars), 3),
            }
        )

    return hints


def _cluster_1d(values: list[float], threshold: float = 105.0) -> list[list[float]]:
    if not values:
        return []
    groups: list[list[float]] = [[min(values)]]
    for value in sorted(values)[1:]:
        if value - statistics.mean(groups[-1]) <= threshold:
            groups[-1].append(value)
        else:
            groups.append([value])
    return groups


def _local_column_hint(hints: list[dict]) -> int:
    """Very cheap geometry hint. Vision remains authoritative for transitions."""
    candidates = []
    for hint in hints:
        text = hint["text"]
        x0, y0, x1, y1 = hint["bbox"]
        width = x1 - x0
        if sum(ch.isalpha() for ch in text) < 60:
            continue
        # Exclude obviously full-width title/abstract-like blocks.
        if width > 760:
            continue
        candidates.append(float(x0))

    groups = [g for g in _cluster_1d(candidates) if len(g) >= 2]
    if len(groups) >= 3:
        return 3
    if len(groups) >= 2:
        return 2
    return 1


def _deterministic_style(pdf_path: Path) -> dict:
    doc = pymupdf.open(pdf_path)
    try:
        width = float(doc[0].rect.width)
        height = float(doc[0].rect.height)
        font_counts: dict[str, int] = {}
        body_sizes: list[float] = []
        left_edges: list[float] = []
        right_edges: list[float] = []
        gap_samples: list[float] = []
        page_column_hints: list[int] = []

        for page in doc:
            hints = _page_hints(page)
            page_column_hints.append(_local_column_hint(hints))

            data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)
            for block in data.get("blocks", []):
                if block.get("type") != 0:
                    continue
                text = _block_text(block).strip()
                if len(text) < 20:
                    continue
                x0, y0, x1, y1 = [float(v) for v in block["bbox"]]
                if y0 > page.rect.height * 0.93:
                    continue

                left_edges.append(x0)
                right_edges.append(page.rect.width - x1)

                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        st = span.get("text", "").strip()
                        if not st:
                            continue
                        font = span.get("font", "") or ""
                        font_counts[font] = font_counts.get(font, 0) + len(st)
                        size = float(span.get("size", 10.0))
                        if 6.0 <= size <= 13.5:
                            body_sizes.extend([size] * min(30, len(st)))

            # Estimate a typical 2-column gap where possible.
            narrow = [
                h for h in hints
                if len(h["text"]) >= 40 and (h["bbox"][2] - h["bbox"][0]) < 600
            ]
            left = [h for h in narrow if h["bbox"][0] < 350]
            right = [h for h in narrow if h["bbox"][0] > 350]
            if left and right:
                gap_norm = min(h["bbox"][0] for h in right) - max(
                    h["bbox"][2] for h in left
                )
                if 5 < gap_norm < 200:
                    gap_samples.append(width * gap_norm / 1000.0)

        def percentile(values: list[float], q: float, default: float) -> float:
            if not values:
                return default
            values = sorted(values)
            idx = int(round((len(values) - 1) * q))
            return values[max(0, min(len(values) - 1, idx))]

        majority_columns = 1
        if page_column_hints:
            majority_columns = max(
                (1, 2, 3),
                key=lambda n: page_column_hints.count(n),
            )

        return {
            "page_width_pt": width,
            "page_height_pt": height,
            "columns": majority_columns,
            "page_column_hints": page_column_hints,
            "column_gap_pt": (
                max(10.0, min(34.0, statistics.median(gap_samples)))
                if gap_samples
                else 18.0
            ),
            "left_margin_pt": max(
                38.0, min(80.0, percentile(left_edges, 0.08, 55.0))
            ),
            "right_margin_pt": max(
                38.0, min(80.0, percentile(right_edges, 0.08, 55.0))
            ),
            "top_margin_pt": 46.0,
            "bottom_margin_pt": 48.0,
            "body_size_pt": max(
                8.0,
                min(11.0, statistics.median(body_sizes) if body_sizes else 9.4),
            ),
            "font_summary": sorted(
                font_counts.items(), key=lambda item: item[1], reverse=True
            )[:12],
        }
    finally:
        doc.close()


def _document_text_preview(pdf_path: Path, max_chars: int = 15000) -> str:
    """Lightweight domain scan: title/abstract/front matter + likely headings."""
    doc = pymupdf.open(pdf_path)
    try:
        chunks: list[str] = []
        for pno in range(min(2, doc.page_count)):
            chunks.append(doc[pno].get_text("text")[:6500])

        heading_candidates: list[str] = []
        for page in doc:
            data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)
            spans = [
                span
                for block in data.get("blocks", [])
                if block.get("type") == 0
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if span.get("text", "").strip()
            ]
            sizes = [float(span.get("size", 10.0)) for span in spans]
            body = statistics.median(sizes) if sizes else 10.0

            for span in spans:
                text = " ".join(span.get("text", "").split())
                size = float(span.get("size", 10.0))
                if (
                    3 <= len(text) <= 140
                    and size >= body * 1.13
                    and text not in heading_candidates
                ):
                    heading_candidates.append(text)

        chunks.append("\nLIKELY HEADINGS:\n" + "\n".join(heading_candidates[:80]))
        return "\n\n".join(chunks)[:max_chars]
    finally:
        doc.close()


TERM_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "target": {"type": "string"},
        "policy": {
            "type": "string",
            "enum": ["translate", "keep_english"],
        },
        "note": {"type": "string"},
    },
    "required": ["source", "target", "policy", "note"],
    "additionalProperties": False,
}

DOCUMENT_SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {"type": "integer", "minimum": 1, "maximum": 3},
        "title_full_width": {"type": "boolean"},
        "body_family": {"type": "string", "enum": ["serif", "sans"]},
        "title_family": {"type": "string", "enum": ["serif", "sans"]},
        "heading_family": {"type": "string", "enum": ["serif", "sans"]},
        "title_alignment": {"type": "string", "enum": ["left", "center"]},
        "title_color": {"type": "string"},
        "body_size_pt": {"type": "number", "minimum": 7.0, "maximum": 13.0},
        "title_size_pt": {"type": "number", "minimum": 12.0, "maximum": 30.0},
        "section_size_pt": {"type": "number", "minimum": 8.0, "maximum": 18.0},
        "line_spacing": {"type": "number", "minimum": 0.9, "maximum": 1.35},
        "paragraph_indent_em": {"type": "number", "minimum": 0.0, "maximum": 2.5},
        "abstract_style": {"type": "string", "enum": ["normal", "bold", "italic"]},
        "section_weight": {"type": "string", "enum": ["normal", "bold"]},
        "footer_rule": {"type": "boolean"},
        "page_number_position": {
            "type": "string",
            "enum": ["outer", "right", "center"],
        },
        "field": {"type": "string"},
        "subfield": {"type": "string"},
        "document_type": {"type": "string"},
        "register": {"type": "string"},
        "terminology": {"type": "array", "items": TERM_SCHEMA},
        "translation_principles": {
            "type": "array",
            "items": {"type": "string"},
        },
        "do_not_translate": {"type": "array", "items": {"type": "string"}},
        "math_notation_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "columns",
        "title_full_width",
        "body_family",
        "title_family",
        "heading_family",
        "title_alignment",
        "title_color",
        "body_size_pt",
        "title_size_pt",
        "section_size_pt",
        "line_spacing",
        "paragraph_indent_em",
        "abstract_style",
        "section_weight",
        "footer_rule",
        "page_number_position",
        "field",
        "subfield",
        "document_type",
        "register",
        "terminology",
        "translation_principles",
        "do_not_translate",
        "math_notation_notes",
    ],
    "additionalProperties": False,
}


def analyze_document(pdf_path: Path, target_language: str) -> tuple[dict, dict]:
    """One light scan replaces the old separate style-only call."""
    metrics = _deterministic_style(pdf_path)

    if os.getenv("MOCK_VISION", "false").lower() == "true":
        result = {
            "columns": metrics["columns"],
            "title_full_width": True,
            "body_family": "serif",
            "title_family": "sans",
            "heading_family": "sans",
            "title_alignment": "left",
            "title_color": "#5B347C",
            "body_size_pt": metrics["body_size_pt"],
            "title_size_pt": 20.0,
            "section_size_pt": 11.5,
            "line_spacing": 1.04,
            "paragraph_indent_em": 1.0,
            "abstract_style": "bold",
            "section_weight": "normal",
            "footer_rule": True,
            "page_number_position": "right",
            "field": "scientific research",
            "subfield": "unknown",
            "document_type": "research paper",
            "register": "formal academic",
            "terminology": [],
            "translation_principles": [
                "Translate only broadly standardized concepts; keep niche technical concept names in English when a Korean rendering would be awkward or nonstandard."
            ],
            "do_not_translate": ["author names", "DOIs", "URLs", "equation labels"],
            "math_notation_notes": [
                "Preserve all mathematical notation exactly."
            ],
        }
    else:
        doc = pymupdf.open(pdf_path)
        try:
            indices = []
            for idx in [0, max(0, doc.page_count // 2)]:
                if idx < doc.page_count and idx not in indices:
                    indices.append(idx)
            images = [(_render_page(doc[i], 105), "image/jpeg") for i in indices]
        finally:
            doc.close()

        preview = _document_text_preview(pdf_path)

        prompt = (
            "You are doing a LIGHT PRE-SCAN before a scientific PDF is reconstructed and translated.\n"
            f"Target language: {LANGUAGE_NAMES.get(target_language, target_language)}.\n"
            "The attached representative page images and extracted front-matter/headings belong "
            "to one document.\n\n"
            "Do two things in one response:\n"
            "A) infer the publication's typographic grammar: usual column count, title spanning, "
            "serif/sans roles, title alignment/color, approximate type sizes, paragraph indent, "
            "line spacing, abstract emphasis, section weight, footer/page-number style;\n"
            "B) identify the academic field/subfield and prepare a translation strategy BEFORE "
            "the body translation starts.\n\n"
            "For the terminology strategy:\n"
            "- Classify each technical concept with policy=translate or policy=keep_english.\n"
            "- For KOREAN targets, translate only concepts whose Korean name is broadly standardized "
            "and recognizable across adjacent fields or standard textbooks. Examples include entropy, "
            "density matrix, Hamiltonian, quantum state, relative entropy, and measurement.\n"
            "- For niche, recently coined, subfield-specific, protocol/model/resource-theory terms, "
            "or any concept whose Korean rendering sounds artificial or is not broadly standardized, "
            "prefer policy=keep_english and keep the source English term unchanged.\n"
            "- Do NOT invent Korean transliterations merely because a term can be transliterated. "
            "For example, a narrow term such as 'ergotropy' should normally remain 'ergotropy' "
            "unless the document itself establishes another convention.\n"
            "- When policy=keep_english, set target equal to the source English term. Do not append "
            "a parenthesized Korean gloss unless the source already contains one.\n"
            "- Prefer field-standard translations over literal word-for-word translations for the "
            "terms that genuinely should be translated.\n"
            "- Include ambiguous/high-value recurring terms, not ordinary vocabulary.\n"
            "- Keep notation, variable names, acronyms, author names, bibliographic titles, DOIs and URLs unchanged.\n"
            "- For Korean scientific prose, avoid mechanical particles such as '은(는)', '이(가)', "
            "'을(를)'; rewrite the sentence naturally instead.\n"
            "- A term such as 'measurement' in quantum mechanics normally means the physical "
            "measurement operation, not a measure-theory 'measure'.\n"
            "- Return a concise glossary; roughly 10-30 terms is enough.\n\n"
            "Deterministic PDF geometry/font measurements:\n"
            + json.dumps(metrics, ensure_ascii=False)
            + "\n\nLightweight text scan:\n"
            + preview
        )

        result = _call_json(prompt, images, DOCUMENT_SCAN_SCHEMA, "document-scan")

    style_keys = {
        "columns",
        "title_full_width",
        "body_family",
        "title_family",
        "heading_family",
        "title_alignment",
        "title_color",
        "body_size_pt",
        "title_size_pt",
        "section_size_pt",
        "line_spacing",
        "paragraph_indent_em",
        "abstract_style",
        "section_weight",
        "footer_rule",
        "page_number_position",
    }
    style = {key: result[key] for key in style_keys}

    style.update(
        {
            "page_width_pt": metrics["page_width_pt"],
            "page_height_pt": metrics["page_height_pt"],
            "left_margin_pt": metrics["left_margin_pt"],
            "right_margin_pt": metrics["right_margin_pt"],
            "top_margin_pt": metrics["top_margin_pt"],
            "bottom_margin_pt": metrics["bottom_margin_pt"],
            "column_gap_pt": metrics["column_gap_pt"],
            "page_column_hints": metrics["page_column_hints"],
        }
    )

    # Use visual analysis for style, but do not allow it to erase a strong
    # deterministic multi-column majority.
    if metrics["columns"] > 1 and result["columns"] == 1:
        style["columns"] = metrics["columns"]

    color = str(style.get("title_color", "#333333")).strip()
    if not color.startswith("#") or len(color) != 7:
        color = "#333333"
    style["title_color"] = color

    strategy = {
        "field": result["field"],
        "subfield": result["subfield"],
        "document_type": result["document_type"],
        "register": result["register"],
        "terminology": result["terminology"][:30],
        "translation_principles": result["translation_principles"][:12],
        "do_not_translate": result["do_not_translate"][:20],
        "math_notation_notes": result["math_notation_notes"][:12],
    }

    print(
        "Document scan: "
        f"field={strategy['field']} / {strategy['subfield']}; "
        f"default_columns={style['columns']}; "
        f"glossary_terms={len(strategy['terminology'])}",
        flush=True,
    )

    if strategy["terminology"]:
        preview_terms = ", ".join(
            (
                f"{term['source']} -> {term['target']}"
                if term.get("policy") == "translate"
                else f"{term['source']} [keep English]"
            )
            for term in strategy["terminology"][:10]
        )
        print(f"Terminology strategy: {preview_terms}", flush=True)

    return style, strategy


MATH_BACKSLASH_TOKEN = "§"


def _decode_math_transport(value: str) -> str:
    """Decode JSON-safe math transport and contextually repair JSON escapes."""
    value = str(value or "")
    value = value.replace(MATH_BACKSLASH_TOKEN, "\\")

    # BACKSPACE / FORM FEED are not normal math formatting.
    value = value.replace("\x08", r"\b")
    value = value.replace("\x0c", r"\f")

    def repair_control(
        text: str,
        control: str,
        command_prefix: str,
        tail_pattern: str,
    ) -> str:
        pattern = re.compile(re.escape(control) + rf"(?={tail_pattern})")
        text = pattern.sub(lambda _m: "\\" + command_prefix, text)
        # Unrecognized controls are indentation/formatting whitespace.
        return text.replace(control, " ")

    # \text..., \theta, \times, \top, \tilde, \tau, \triangle, \tfrac, \to
    value = repair_control(
        value,
        "\t",
        "t",
        r"(?:ext(?:sf|bf|it|tt|rm|normal)?\b|heta\b|imes\b|op\b|"
        r"ilde\b|au\b|riangle\w*\b|frac\b|o\b)",
    )

    # \rho, \right, \rangle, \rvert, \rm
    value = repair_control(
        value,
        "\r",
        "r",
        r"(?:ho\b|ight\b|angle\b|vert\b|m\b)",
    )

    # \nabla, \neq, \nu, \notin, \neg
    value = repair_control(
        value,
        "\n",
        "n",
        r"(?:abla\b|eq\b|u\b|otin\b|eg\b)",
    )

    return value


MATH_REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "math": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["math", "confidence"],
    "additionalProperties": False,
}


def repair_math_formula(
    latex: str,
    label: str = "",
    source_image: bytes | None = None,
    compiler_error: str = "",
) -> str:
    """Repair one formula using the source image whenever available.

    The image is authoritative. The current LaTeX and compiler error are only
    hints. This prevents a syntax-repair model from preserving hallucinated
    custom macros such as ``\\fn`` merely because they appeared in its input.
    """
    transport = str(latex or "").replace("\\", MATH_BACKSLASH_TOKEN)

    prompt = (
        "You are repairing ONE mathematical expression transcribed from a scientific PDF.\n"
        "The attached source crop, when present, is AUTHORITATIVE. Read the formula from the image.\n"
        "The supplied LaTeX is only a fallible transcription hint.\n\n"
        "Requirements:\n"
        "- Preserve the mathematical content visible in the image exactly.\n"
        "- Do not translate, simplify, rename variables, change indices, or invent notation.\n"
        "- Use only standard LaTeX plus amsmath/amssymb commands. NO custom/undefined macros.\n"
        "- NEVER emit two subscripts or two superscripts on the same TeX atom. "
        "Forms like A_{x}_{y} and A^{x}^{y} are invalid. Read the source image to determine nesting. "
        "If the second script visually belongs to the already-scripted object, group that object explicitly, "
        "for example {A_{x}}_{y}; if it belongs inside the first script, write A_{x_{y}}.\n"
        "- If visible roman letters occur as a superscript/subscript/label, use §mathrm{...} or §text{...}.\n"
        "  Example: visible superscript 'fn' -> ^{§mathrm{fn}}, never ^{§fn}.\n"
        "- Preserve alignment relations, but return only the equation body: no equation/aligned environment.\n"
        "- Use § instead of EVERY LaTeX backslash in the JSON output.\n"
        f"Context label: {label}\n"
        f"XeLaTeX error:\n{compiler_error[-1800:]}\n\n"
        f"Current transcription:\n{transport}"
    )

    images = [(source_image, "image/jpeg")] if source_image else []
    result = _call_json(
        prompt,
        images,
        MATH_REPAIR_SCHEMA,
        "math-source-repair",
    )

    repaired = _decode_math_transport(str(result.get("math", ""))).strip()
    if not repaired:
        raise GeminiVisionError(f"Math repair returned empty output: {label}")

    return repaired


def _brace_balance_error(math: str) -> str | None:
    """Conservative TeX group balance check, ignoring escaped literal braces."""
    depth = 0
    i = 0

    while i < len(math):
        ch = math[i]

        if ch == "\\":
            # \{ and \} print literal braces and do not open/close a TeX group.
            if i + 1 < len(math) and math[i + 1] in "{}":
                i += 2
                continue

            i += 1
            while i < len(math) and math[i].isalpha():
                i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return "extra closing brace"

        i += 1

    if depth:
        return f"{depth} unmatched opening brace(s)"
    return None


def _validate_math_transport(math: str, page_number: int, label: str) -> None:
    decoded = _decode_math_transport(math).strip()
    if not decoded:
        raise GeminiVisionError(f"Page {page_number}: empty LaTeX in {label}")

    error = _brace_balance_error(decoded)
    if error:
        raise GeminiVisionError(
            f"Page {page_number}: malformed LaTeX in {label}: "
            f"{error}: {decoded[:180]}"
        )


PART_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["text", "math"]},
        "content": {"type": "string"},
    },
    "required": ["type", "content"],
    "additionalProperties": False,
}

TABLE_CELL_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {"type": "array", "items": PART_SCHEMA},
        "style": {
            "type": "string",
            "enum": ["normal", "bold", "italic", "smallcaps"],
        },
        "colspan": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
        },
        "align": {
            "type": "string",
            "enum": ["left", "center", "right"],
        },
    },
    "required": ["parts", "style", "colspan", "align"],
    "additionalProperties": False,
}

TABLE_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "cells": {
            "type": "array",
            "items": TABLE_CELL_SCHEMA,
        },
    },
    "required": ["cells"],
    "additionalProperties": False,
}


BLOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "title",
                "author",
                "affiliation",
                "metadata",
                "abstract",
                "section",
                "subsection",
                "paragraph",
                "list_item",
                "equation",
                "figure",
                "table",
                "caption",
                "reference",
                "footer",
            ],
        },
        "flow_columns": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3,
        },
        "column": {
            "type": "string",
            "enum": ["full", "column1", "column2", "column3", "auto"],
        },
        "translate": {"type": "boolean"},
        "style": {
            "type": "string",
            "enum": ["normal", "bold", "italic", "smallcaps"],
        },
        "parts": {"type": "array", "items": PART_SCHEMA},
        "equation_latex": {"type": "string"},
        "equation_lines": {
            "type": "array",
            "items": {"type": "string"},
        },
        "equation_number": {"type": "string"},
        "table_header_rows": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
        },
        "table_alignments": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["left", "center", "right"],
            },
            "maxItems": 20,
        },
        "table_rows": {
            "type": "array",
            "items": TABLE_ROW_SCHEMA,
        },
        "bbox": {
            "type": "array",
            "items": {"type": "number", "minimum": 0, "maximum": 1000},
            "minItems": 4,
            "maxItems": 4,
        },
    },
    "required": [
        "kind",
        "flow_columns",
        "column",
        "translate",
        "style",
        "parts",
        "equation_latex",
        "equation_lines",
        "equation_number",
        "table_header_rows",
        "table_alignments",
        "table_rows",
        "bbox",
    ],
    "additionalProperties": False,
}


def _batch_schema(count: int) -> dict:
    page_item = {
        "type": "object",
        "properties": {
            "page_number": {"type": "integer"},
            "blocks": {"type": "array", "items": BLOCK_SCHEMA},
        },
        "required": ["page_number", "blocks"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": page_item,
                "minItems": count,
                "maxItems": count,
            }
        },
        "required": ["pages"],
        "additionalProperties": False,
    }


def _alphabetic_count(text: str) -> int:
    return sum(ch.isalpha() for ch in text)



def _bbox_intersection_area(a: list[float], b: list[float]) -> float:
    if len(a) != 4 or len(b) != 4:
        return 0.0
    x0 = max(float(a[0]), float(b[0]))
    y0 = max(float(a[1]), float(b[1]))
    x1 = min(float(a[2]), float(b[2]))
    y1 = min(float(a[3]), float(b[3]))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)



def _unicode_letter_words(text: str) -> list[str]:
    """Extract Unicode letter-only words without requiring third-party regex."""
    return re.findall(r"[^\W\d_]+", str(text or ""), flags=re.UNICODE)


def _overlapping_hint_text(block: dict, hints: list[dict]) -> str:
    bbox = block.get("bbox") or []
    if len(bbox) != 4:
        return ""

    matches: list[tuple[float, str]] = []
    for hint in hints:
        area = _bbox_intersection_area(bbox, hint.get("bbox", []))
        if area > 0:
            matches.append((area, str(hint.get("text", ""))))

    matches.sort(key=lambda item: item[0], reverse=True)
    return " ".join(text for _area, text in matches[:4])


def _repair_section_sign_glyphs_from_source(
    text: str,
    source_hint: str,
) -> str:
    """Repair OCR/Vision confusions such as `§afránek` -> `Šafránek`.

    The JSON-safe math protocol legitimately uses §, so we cannot globally
    replace it. A prose occurrence is repaired only when the source PDF text
    layer contains a Unicode word whose suffix exactly matches the Vision word.

    Example:
        Vision: Dominik §afránek
        PDF text hint: Dominik Šafránek
        result: Dominik Šafránek
    """
    text = str(text or "")
    if "§" not in text or not source_hint:
        return text

    source_words = _unicode_letter_words(source_hint)
    if not source_words:
        return text

    # § followed by a Unicode letter word. This intentionally does not match
    # section-sign references such as "§ 3" or mathematical wrappers §math{...}.
    pattern = re.compile(r"§([^\W\d_]+)", flags=re.UNICODE)

    def replacement(match: re.Match) -> str:
        tail = match.group(1)

        # Internal transport commands are not prose glyph errors.
        if tail.casefold() in {
            "math", "mathcal", "mathbf", "boldsymbol", "mathrm", "text",
            "textsf", "textbf", "textit", "operatorname", "frac", "sqrt",
            "rho", "gamma", "theta", "sigma", "pi", "phi", "psi", "omega",
            "alpha", "beta", "delta", "lambda", "mu", "nu", "tau",
        }:
            return match.group(0)

        candidates = [
            word
            for word in source_words
            if len(word) == len(tail) + 1
            and word[1:].casefold() == tail.casefold()
        ]

        if len(candidates) == 1:
            return candidates[0]

        return match.group(0)

    return pattern.sub(replacement, text)


def _repair_prose_glyphs_from_source(
    blocks: list[dict],
    hints: list[dict],
) -> None:
    """Repair high-confidence prose glyph OCR errors before later processing."""
    for block in blocks:
        if block.get("kind") in {"equation", "figure", "table"}:
            continue

        source_hint = _overlapping_hint_text(block, hints)
        if not source_hint:
            continue

        parts = block.get("parts") or []
        for part in parts:
            if part.get("type") != "text":
                continue

            content = str(part.get("content", ""))
            repaired = _repair_section_sign_glyphs_from_source(
                content,
                source_hint,
            )
            part["content"] = repaired


def _apply_source_font_weight(
    blocks: list[dict],
    hints: list[dict],
) -> None:
    """Make the actual source PDF font weight authoritative."""
    for block in blocks:
        if block.get("kind") in {"equation", "figure", "table", "footer"}:
            continue

        bbox = block.get("bbox") or []
        if len(bbox) != 4:
            continue

        weighted_bold = 0.0
        weighted_italic = 0.0
        total = 0.0

        for hint in hints:
            area = _bbox_intersection_area(bbox, hint.get("bbox", []))
            if area <= 0:
                continue

            weight = max(1.0, min(area, 50000.0))
            weighted_bold += weight * float(hint.get("bold_ratio", 0.0))
            weighted_italic += weight * float(hint.get("italic_ratio", 0.0))
            total += weight

        if total <= 0:
            continue

        bold_ratio = weighted_bold / total
        italic_ratio = weighted_italic / total

        if bold_ratio >= 0.62:
            block["style"] = "bold"
        elif italic_ratio >= 0.62:
            block["style"] = "italic"
        elif bold_ratio <= 0.18 and italic_ratio <= 0.18:
            block["style"] = "normal"


def _validate_blocks(
    blocks: list[dict],
    hints: list[dict],
    page_number: int,
    local_hint: int,
) -> list[dict]:
    if not isinstance(blocks, list) or not blocks:
        raise GeminiVisionError(f"Page {page_number}: vision agent returned no blocks")

    src_letters = sum(_alphabetic_count(hint["text"]) for hint in hints)
    out_letters = 0
    equation_count = 0
    has_multi_flow = False

    for block in blocks:
        kind = block.get("kind")
        flow = int(block.get("flow_columns", 1))
        if flow > 1:
            has_multi_flow = True

        if kind == "equation":
            latex = str(block.get("equation_latex", "")).strip()
            lines = block.get("equation_lines") or []
            if not latex and not any(str(line).strip() for line in lines):
                raise GeminiVisionError(
                    f"Page {page_number}: equation block has no LaTeX"
                )

            if latex:
                _validate_math_transport(
                    latex,
                    page_number,
                    "equation_latex",
                )

            for line_index, line in enumerate(lines):
                if str(line).strip():
                    _validate_math_transport(
                        str(line),
                        page_number,
                        f"equation_lines[{line_index}]",
                    )

            equation_count += 1
        elif kind == "table":
            rows = block.get("table_rows") or []
            if not rows:
                raise GeminiVisionError(
                    f"Page {page_number}: table block has no semantic rows"
                )

            row_widths: list[int] = []
            for row_index, row in enumerate(rows):
                cells = row.get("cells") or []
                if not cells:
                    raise GeminiVisionError(
                        f"Page {page_number}: table row {row_index} has no cells"
                    )

                logical_width = 0
                for cell_index, cell in enumerate(cells):
                    logical_width += max(1, int(cell.get("colspan", 1)))
                    parts = cell.get("parts")
                    if not isinstance(parts, list):
                        raise GeminiVisionError(
                            f"Page {page_number}: invalid table cell parts"
                        )

                    for part in parts:
                        if part.get("type") == "text":
                            out_letters += _alphabetic_count(
                                str(part.get("content", ""))
                            )
                        elif part.get("type") == "math":
                            math = str(part.get("content", "")).strip()
                            if not math:
                                raise GeminiVisionError(
                                    f"Page {page_number}: empty table-cell math"
                                )
                            _validate_math_transport(
                                math,
                                page_number,
                                f"table row {row_index} cell {cell_index}",
                            )

                row_widths.append(logical_width)

            column_count = max(row_widths)
            if column_count < 1 or column_count > 20:
                raise GeminiVisionError(
                    f"Page {page_number}: invalid table width {column_count}"
                )
            if any(width != column_count for width in row_widths):
                raise GeminiVisionError(
                    f"Page {page_number}: inconsistent table row widths {row_widths}"
                )

            alignments = block.get("table_alignments") or []
            if alignments and len(alignments) != column_count:
                raise GeminiVisionError(
                    f"Page {page_number}: table_alignments has "
                    f"{len(alignments)} entries for {column_count} columns"
                )

            header_rows = int(block.get("table_header_rows", 0) or 0)
            if header_rows < 0 or header_rows > len(rows):
                raise GeminiVisionError(
                    f"Page {page_number}: invalid table_header_rows={header_rows}"
                )

        elif kind != "figure":
            parts = block.get("parts")
            if not isinstance(parts, list):
                raise GeminiVisionError(
                    f"Page {page_number}: invalid parts in {kind}"
                )
            for part in parts:
                if part.get("type") == "text":
                    out_letters += _alphabetic_count(str(part.get("content", "")))
                elif part.get("type") == "math":
                    math = str(part.get("content", "")).strip()
                    if not math:
                        raise GeminiVisionError(
                            f"Page {page_number}: empty inline math"
                        )
                    _validate_math_transport(
                        math,
                        page_number,
                        f"inline math in {kind}",
                    )

        bbox = block.get("bbox", [])
        if len(bbox) != 4:
            raise GeminiVisionError(f"Page {page_number}: invalid bbox")

    _repair_prose_glyphs_from_source(blocks, hints)
    _apply_source_font_weight(blocks, hints)

    if src_letters >= 500 and out_letters < src_letters * 0.46:
        raise GeminiVisionError(
            f"Page {page_number}: vision transcription appears incomplete "
            f"({out_letters}/{src_letters} alphabetic chars)"
        )

    # If geometry clearly sees multiple columns but every returned block says
    # one column, correct the obvious collapse. Mixed layouts are left alone.
    if local_hint > 1 and not has_multi_flow:
        for block in blocks:
            if block.get("column") != "full":
                block["flow_columns"] = local_hint

    print(
        f"Vision page {page_number}: blocks={len(blocks)}, "
        f"display_equations={equation_count}, geometry_columns~{local_hint}",
        flush=True,
    )
    return blocks


def parse_pages(
    pdf_path: Path,
    page_indices: list[int],
    style: dict,
    strategy: dict,
) -> dict[int, list[dict]]:
    if not page_indices:
        return {}

    doc = pymupdf.open(pdf_path)
    try:
        page_payloads = []
        images = []
        for page_index in page_indices:
            page = doc[page_index]
            hints = _page_hints(page)
            page_payloads.append(
                {
                    "page_number": page_index + 1,
                    "geometry_column_hint": _local_column_hint(hints),
                    "hints": hints,
                }
            )
            images.append((_render_page(page, 132), "image/jpeg"))
    finally:
        doc.close()

    if os.getenv("MOCK_VISION", "false").lower() == "true":
        result = {}
        for payload in page_payloads:
            blocks = []
            flow = payload["geometry_column_hint"]
            for hint in payload["hints"]:
                blocks.append(
                    {
                        "kind": "paragraph",
                        "flow_columns": flow,
                        "column": "auto",
                        "translate": True,
                        "style": "normal",
                        "parts": [{"type": "text", "content": hint["text"]}],
                        "equation_latex": "",
                        "equation_lines": [],
                        "equation_number": "",
                        "table_header_rows": 0,
                        "table_alignments": [],
                        "table_rows": [],
                        "bbox": hint["bbox"],
                    }
                )
            result[payload["page_number"] - 1] = blocks
        return result

    page_numbers = [index + 1 for index in page_indices]

    prompt = (
        "You are the page-reconstruction agent for a scientific PDF translation system.\n"
        f"The attached images, IN ORDER, are source pages {page_numbers}.\n"
        "Each page image is authoritative. Extracted PDF text below is only a hint for wording "
        "and can contain broken line endings or damaged equation extraction.\n\n"
        "Reconstruct each page into semantic blocks in actual READING ORDER.\n\n"
        "CRITICAL MATHEMATICS RULES:\n"
        "1. DO NOT TRANSLATE anything in this stage.\n"
        "1A. JSON-SAFE LATEX TRANSPORT: in every math field use the literal character § "
        "instead of every LaTeX backslash. NEVER emit a literal backslash inside inline math, "
        "equation_latex, or equation_lines. Examples: §rho, §frac{a}{b}, "
        "§boldsymbol{§textsf{C}}^d, S_{§mathcal M,§gamma}^{(j)}. "
        "The renderer converts § back to a real LaTeX backslash after JSON parsing.\n"
        "2. Every inline mathematical expression MUST be its own part with type=math and VALID LaTeX "
        "under this § transport convention. Never place §rho, §gamma, §Pi_y, §mathcal{M}, "
        "C_{§mathcal M}, D(§rho§Vert§gamma), or any other LaTeX transport syntax inside "
        "a type=text part. Split surrounding prose into text/math/text parts. "
        "without $ delimiters. Natural prose must remain type=text. "
        "IMPORTANT: § is reserved ONLY as a LaTeX-backslash transport character inside math fields. "
        "Never use § as a substitute for a real Unicode prose letter. Preserve names exactly from "
        "the page/text hints: for example `Šafránek` must stay `Šafránek`, never `§afránek`.\n"
        "3. Read superscripts and subscripts from their VISUAL vertical position. "
        "Never flatten them into baseline text. Use ^ and _ explicitly; use braces for multi-token "
        "scripts. Using the § transport convention, examples are "
        "P_R^§gamma, S_{§mathcal M,§gamma}^{(j)}, §rho^T, q_y, §gamma_x, Q_R^§gamma.\n"
        "4. Preserve bra-ket notation correctly, e.g. "
        "\\lvert\\psi_x\\rangle\\langle\\psi_x\\rvert, not OCR-like '|ψxihψx|'.\n"
        "5. Preserve operator typography: \\operatorname{Tr}, \\mathcal M, \\Vert, \\otimes, "
        "\\sqrt{}, \\sum, \\dagger, inverse powers, transpose powers, primes, and accents.\n"
        "6. Every standalone/display equation is kind=equation and MUST be represented as LaTeX, "
        "never as a figure or cropped image.\n"
        "7. equation_latex contains the entire mathematical expression without an equation "
        "environment. equation_lines contains mathematically sensible line pieces. For a short "
        "equation, use a one-element array. For a long equation, split at relations (=, :=, "
        "\\Leftrightarrow) or top-level + / - terms so each line can fit a narrow journal column. "
        "Do not split inside a fraction, radical, exponent, subscript, bra-ket, or paired delimiter.\n"
        "8. Keep the printed equation number separately in equation_number.\n"
        "8A. Use ONLY standard LaTeX/amsmath/amssymb commands. Never invent custom macros "
        "such as §fn, §clax, §op or source-defined shorthand that would require a preamble definition. "
        "If the image shows a short roman-text superscript/subscript or label, encode the visible letters "
        "explicitly with §mathrm{...} or §text{...}; for example a visible superscript fn should be "
        "^{§mathrm{fn}}, not ^{§fn}. If uncertain, match the image literally using standard commands.\n"
        "8B. Do not guess abbreviations or silently correct the mathematics. The page image is authoritative.\n\n"
        "LAYOUT RULES:\n"
        "9. flow_columns is the NUMBER OF TEXT COLUMNS in the LOCAL PAGE REGION containing this "
        "block (1, 2, or 3). This is not necessarily constant across a document or even a page.\n"
        "10. A title/figure/equation can span full width while the surrounding region has "
        "flow_columns=2 or 3; in that case set column='full' but keep flow_columns equal to the "
        "surrounding flow count.\n"
        "11. If the source visibly changes 2 columns -> 1 column, 1 -> 2, 2 -> 3, etc., assign "
        "the new flow_columns value to blocks after that transition. Do not force the global "
        "document default onto every block.\n"
        "12. For column labels use column1/column2/column3/full/auto. Exact paragraph coordinates "
        "do not need to be reproduced, but the number of columns and full-width regions do.\n\n"
        "CONTENT RULES:\n"
        "12A. Preserve source font emphasis. A block whose extracted bold_ratio is near 1 "
        "must remain bold even when there is no explicit 'Abstract' label. Conversely, do not "
        "make a regular-weight sans-serif heading bold solely because it is a heading.\n\n"
        "13. Preserve hierarchy: title, author, affiliation, metadata, abstract, section, "
        "subsection, paragraph, list_item, equation, figure, table, caption, reference, footer.\n"
        "14. Authors, affiliations, journal/arXiv metadata, emails, references and footer material "
        "normally use translate=false. Preserve their spelling and Unicode diacritics exactly, using "
        "the extracted PDF text hint when it is clearer than the image. Title, abstract, headings, "
        "prose and captions normally use translate=true.\n"
        "15. TABLES MUST NEVER BE RETURNED AS FIGURES OR CROPPED IMAGES. Every visible table, "
        "including a rasterized/scanned table, must be kind=table and reconstructed semantically. "
        "Populate table_rows with every visible row/cell, table_header_rows with the number of "
        "header rows at the top, and table_alignments with exactly one left/center/right entry per "
        "logical column. Cell contents use text/math parts exactly like prose. Use colspan for "
        "horizontally merged cells. Preserve bold/italic cell emphasis. A table caption or table "
        "footnote is a separate caption/paragraph block, never part of table_rows. For every "
        "non-table block return table_header_rows=0, table_alignments=[], table_rows=[].\n"
        "15A. FIGURES use kind=figure. Their bbox is normalized [x0,y0,x1,y1] in 0..1000 and "
        "must tightly cover only the visual object, excluding its separate caption. Do not convert "
        "equations or tables into figure blocks.\n"
        "16. Omit page-number-only items. Do not omit dense prose, equations, or table cells.\n\n"
        "Document domain scan (helps interpret notation but MUST NOT alter source content):\n"
        + json.dumps(
            {
                "field": strategy.get("field"),
                "subfield": strategy.get("subfield"),
                "math_notation_notes": strategy.get("math_notation_notes", []),
            },
            ensure_ascii=False,
        )
        + "\n\nGlobal style profile:\n"
        + json.dumps(style, ensure_ascii=False)
        + "\n\nPer-page extracted hints:\n"
        + json.dumps(page_payloads, ensure_ascii=False)
    )

    last_error: Exception | None = None
    for retry in range(2):
        try:
            response = _call_json(
                prompt
                + (
                    "\n\nRETRY NOTE: the previous reconstruction failed validation. "
                    "Pay special attention to omitted text, superscript/subscript placement, "
                    "display equations, and local column transitions."
                    if retry
                    else ""
                ),
                images,
                _batch_schema(len(page_indices)),
                "pages-" + "-".join(str(n) for n in page_numbers),
            )

            pages = response.get("pages")
            if not isinstance(pages, list) or len(pages) != len(page_indices):
                raise GeminiVisionError(
                    f"Expected {len(page_indices)} page results, got "
                    f"{0 if not isinstance(pages, list) else len(pages)}"
                )

            by_number = {}
            for page_result in pages:
                number = int(page_result.get("page_number", -1))
                by_number[number] = page_result.get("blocks")

            if set(by_number) != set(page_numbers):
                raise GeminiVisionError(
                    f"Vision page numbers mismatch: expected {page_numbers}, "
                    f"got {sorted(by_number)}"
                )

            validated: dict[int, list[dict]] = {}
            for payload in page_payloads:
                number = payload["page_number"]
                blocks = _validate_blocks(
                    by_number[number],
                    payload["hints"],
                    number,
                    payload["geometry_column_hint"],
                )
                validated[number - 1] = blocks
            return validated

        except GeminiVisionError as exc:
            last_error = exc
            print(
                f"Vision batch {page_numbers} validation retry: {exc}",
                flush=True,
            )

    raise GeminiVisionError(
        f"Could not faithfully reconstruct pages {page_numbers}: {last_error}"
    )


def parse_page(
    pdf_path: Path,
    page_index: int,
    style: dict,
    strategy: dict,
) -> list[dict]:
    return parse_pages(pdf_path, [page_index], style, strategy)[page_index]
