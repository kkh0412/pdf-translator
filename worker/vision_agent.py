from __future__ import annotations

import base64
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

import pymupdf


class GeminiVisionError(RuntimeError):
    pass


def _model_candidates() -> list[str]:
    primary = os.getenv("GEMINI_VISION_MODEL", "gemini-3.6-flash").strip()
    models = [
        primary,
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ]
    out: list[str] = []
    for m in models:
        if m and m not in out:
            out.append(m)
    return out


def _response_text(response: dict) -> str:
    candidates = response.get("candidates") or []
    if not candidates:
        raise GeminiVisionError(f"Gemini returned no candidates: {response}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if p.get("text"))
    if not text:
        raise GeminiVisionError("Gemini returned no text output")
    return text


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
        for attempt in range(2):
            parts = [{"text": prompt}]
            for data, mime in images:
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": mime,
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    }
                )

            body = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "temperature": 0.0,
                    "thinkingConfig": {"thinkingLevel": "low"},
                    "responseFormat": {
                        "text": {
                            "mimeType": "application/json",
                            "schema": schema,
                        }
                    },
                },
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
                    f"Vision agent {label}: model={model}, attempt={attempt + 1}",
                    flush=True,
                )
                with urllib.request.urlopen(request, timeout=180) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return json.loads(_response_text(payload))

            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"Gemini vision HTTP {exc.code}: {detail}")
                if exc.code == 404:
                    break
                if exc.code in {429, 500, 502, 503, 504}:
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

    raise GeminiVisionError(f"Vision agent failed for {label}: {last_error}")


def _render_page(page: pymupdf.Page, dpi: int = 135) -> bytes:
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return pix.tobytes("jpeg", jpg_quality=78)


def _block_text(block: dict) -> str:
    lines: list[str] = []
    for line in block.get("lines", []):
        chunks = [s.get("text", "") for s in line.get("spans", [])]
        line_text = "".join(chunks).strip()
        if line_text:
            lines.append(line_text)
    return "\n".join(lines)


def _page_hints(page: pymupdf.Page) -> list[dict]:
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)
    width = max(1.0, float(page.rect.width))
    height = max(1.0, float(page.rect.height))
    hints: list[dict] = []
    n = 0
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        text = _block_text(block).strip()
        if not text:
            continue
        x0, y0, x1, y1 = [float(v) for v in block["bbox"]]
        spans = [
            s for line in block.get("lines", [])
            for s in line.get("spans", [])
            if s.get("text", "").strip()
        ]
        fonts: dict[str, int] = {}
        sizes: list[float] = []
        for s in spans:
            f = s.get("font", "") or ""
            fonts[f] = fonts.get(f, 0) + max(1, len(s.get("text", "")))
            sizes.append(float(s.get("size", 10.0)))
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
            }
        )
        n += 1
    return hints


def _deterministic_style(pdf_path: Path) -> dict:
    doc = pymupdf.open(pdf_path)
    try:
        width = float(doc[0].rect.width)
        height = float(doc[0].rect.height)
        font_counts: dict[str, int] = {}
        body_sizes: list[float] = []
        left_edges: list[float] = []
        right_edges: list[float] = []
        left_column_ends: list[float] = []
        right_column_starts: list[float] = []
        two_column_votes = 0
        vote_pages = 0

        for pno, page in enumerate(doc):
            data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)
            page_blocks = []
            for block in data.get("blocks", []):
                if block.get("type") != 0:
                    continue
                text = _block_text(block).strip()
                if len(text) < 20:
                    continue
                x0, y0, x1, y1 = [float(v) for v in block["bbox"]]
                if y0 > page.rect.height * 0.92:
                    continue
                page_blocks.append((x0, y0, x1, y1, text))
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
                        if 6.0 <= size <= 13.0:
                            body_sizes.extend([size] * min(30, max(1, len(st))))

            if pno >= 1 and page_blocks:
                left = [b for b in page_blocks if b[2] <= width * 0.56]
                right = [b for b in page_blocks if b[0] >= width * 0.44]
                if len(left) >= 3 and len(right) >= 3:
                    two_column_votes += 1
                    left_column_ends.extend(b[2] for b in left)
                    right_column_starts.extend(b[0] for b in right)
                vote_pages += 1

        columns = 2 if vote_pages and two_column_votes / vote_pages >= 0.45 else 1
        gap = 18.0
        if columns == 2 and left_column_ends and right_column_starts:
            gap = max(
                10.0,
                min(
                    36.0,
                    statistics.median(right_column_starts)
                    - statistics.median(left_column_ends),
                ),
            )

        def pct(vals: list[float], q: float, default: float) -> float:
            if not vals:
                return default
            vals = sorted(vals)
            idx = int(round((len(vals) - 1) * q))
            return vals[max(0, min(len(vals) - 1, idx))]

        return {
            "page_width_pt": width,
            "page_height_pt": height,
            "columns": columns,
            "column_gap_pt": gap,
            "left_margin_pt": max(38.0, min(80.0, pct(left_edges, 0.08, 55.0))),
            "right_margin_pt": max(38.0, min(80.0, pct(right_edges, 0.08, 55.0))),
            "top_margin_pt": 46.0,
            "bottom_margin_pt": 48.0,
            "body_size_pt": max(
                8.0,
                min(11.0, statistics.median(body_sizes) if body_sizes else 9.5),
            ),
            "font_summary": sorted(
                font_counts.items(), key=lambda x: x[1], reverse=True
            )[:12],
        }
    finally:
        doc.close()


STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {"type": "integer", "enum": [1, 2]},
        "title_full_width": {"type": "boolean"},
        "body_family": {"type": "string", "enum": ["serif", "sans"]},
        "title_family": {"type": "string", "enum": ["serif", "sans"]},
        "heading_family": {"type": "string", "enum": ["serif", "sans"]},
        "title_alignment": {"type": "string", "enum": ["left", "center"]},
        "title_color": {"type": "string"},
        "body_size_pt": {"type": "number", "minimum": 7.0, "maximum": 13.0},
        "title_size_pt": {"type": "number", "minimum": 12.0, "maximum": 28.0},
        "section_size_pt": {"type": "number", "minimum": 9.0, "maximum": 17.0},
        "line_spacing": {"type": "number", "minimum": 0.9, "maximum": 1.35},
        "paragraph_indent_em": {"type": "number", "minimum": 0.0, "maximum": 2.5},
        "abstract_style": {
            "type": "string",
            "enum": ["normal", "bold", "italic"],
        },
        "section_weight": {
            "type": "string",
            "enum": ["normal", "bold"],
        },
        "footer_rule": {"type": "boolean"},
        "page_number_position": {
            "type": "string",
            "enum": ["outer", "right", "center"],
        },
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
    ],
    "additionalProperties": False,
}


def analyze_style(pdf_path: Path) -> dict:
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
        }
    else:
        doc = pymupdf.open(pdf_path)
        try:
            count = doc.page_count
            indices: list[int] = []
            for idx in [0, 1, max(0, count // 2), max(0, count - 1)]:
                if idx < count and idx not in indices:
                    indices.append(idx)
            images = [(_render_page(doc[i], 115), "image/jpeg") for i in indices]
        finally:
            doc.close()

        prompt = (
            "You are the typography and publication-style agent for a PDF translation system.\n"
            "The attached images are representative pages of ONE source document.\n"
            "Infer the visual grammar an original author/publisher used. We will later typeset "
            "the translated document from scratch in LaTeX, so focus on STYLE rather than exact "
            "paragraph coordinates or matching page breaks.\n\n"
            "Determine: one/two-column body, whether the title spans full width, serif/sans roles, "
            "title alignment/color, approximate type sizes, paragraph indentation, line spacing, "
            "abstract emphasis, section-heading weight, footer rule, and page-number placement.\n"
            "Do not infer content or translate anything.\n"
            "Critical rule: if the body is visibly two-column, return columns=2.\n\n"
            "Deterministic PDF measurements are supplied only as supporting evidence:\n"
            + json.dumps(metrics, ensure_ascii=False)
        )
        result = _call_json(prompt, images, STYLE_SCHEMA, "style")

    # Geometry from the PDF itself is more reliable than visual estimation.
    result["page_width_pt"] = metrics["page_width_pt"]
    result["page_height_pt"] = metrics["page_height_pt"]
    result["left_margin_pt"] = metrics["left_margin_pt"]
    result["right_margin_pt"] = metrics["right_margin_pt"]
    result["top_margin_pt"] = metrics["top_margin_pt"]
    result["bottom_margin_pt"] = metrics["bottom_margin_pt"]
    result["column_gap_pt"] = metrics["column_gap_pt"]

    # Never let the visual model collapse a confidently detected two-column article.
    if metrics["columns"] == 2:
        result["columns"] = 2

    color = str(result.get("title_color", "#333333")).strip()
    if not color.startswith("#") or len(color) != 7:
        color = "#333333"
    result["title_color"] = color

    print(
        "Style agent result: "
        f"columns={result['columns']}, body={result['body_family']}, "
        f"title={result['title_family']}, accent={result['title_color']}",
        flush=True,
    )
    return result


PART_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["text", "math"]},
        "content": {"type": "string"},
    },
    "required": ["type", "content"],
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
        "column": {
            "type": "string",
            "enum": ["full", "left", "right", "auto"],
        },
        "translate": {"type": "boolean"},
        "style": {
            "type": "string",
            "enum": ["normal", "bold", "italic", "smallcaps"],
        },
        "parts": {"type": "array", "items": PART_SCHEMA},
        "equation_latex": {"type": "string"},
        "equation_number": {"type": "string"},
        "bbox": {
            "type": "array",
            "items": {"type": "number", "minimum": 0, "maximum": 1000},
            "minItems": 4,
            "maxItems": 4,
        },
    },
    "required": [
        "kind",
        "column",
        "translate",
        "style",
        "parts",
        "equation_latex",
        "equation_number",
        "bbox",
    ],
    "additionalProperties": False,
}

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {"type": "array", "items": BLOCK_SCHEMA},
    },
    "required": ["blocks"],
    "additionalProperties": False,
}


def _alphabetic_count(text: str) -> int:
    return sum(ch.isalpha() for ch in text)


def _validate_page_result(result: dict, hints: list[dict], page_number: int) -> list[dict]:
    blocks = result.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise GeminiVisionError(f"Page {page_number}: vision agent returned no blocks")

    src_letters = sum(_alphabetic_count(h["text"]) for h in hints)
    out_letters = 0
    equation_count = 0

    for block in blocks:
        kind = block.get("kind")
        if kind == "equation":
            latex = str(block.get("equation_latex", "")).strip()
            if not latex:
                raise GeminiVisionError(
                    f"Page {page_number}: equation block has no LaTeX"
                )
            equation_count += 1
        elif kind not in {"figure", "table"}:
            parts = block.get("parts")
            if not isinstance(parts, list):
                raise GeminiVisionError(
                    f"Page {page_number}: invalid parts in {kind}"
                )
            for part in parts:
                if part.get("type") == "text":
                    out_letters += _alphabetic_count(str(part.get("content", "")))
                elif part.get("type") == "math" and not str(part.get("content", "")).strip():
                    raise GeminiVisionError(
                        f"Page {page_number}: empty inline math"
                    )

        bbox = block.get("bbox", [])
        if len(bbox) != 4:
            raise GeminiVisionError(f"Page {page_number}: invalid bbox")

    # The page image is authoritative, but dense prose should not disappear.
    # Ignore some difference from headers/footers and formula-heavy blocks.
    if src_letters >= 500 and out_letters < src_letters * 0.48:
        raise GeminiVisionError(
            f"Page {page_number}: vision transcription appears incomplete "
            f"({out_letters}/{src_letters} alphabetic chars)"
        )

    print(
        f"Vision page {page_number}: blocks={len(blocks)}, "
        f"display_equations={equation_count}",
        flush=True,
    )
    return blocks


def parse_page(
    pdf_path: Path,
    page_index: int,
    style: dict,
) -> list[dict]:
    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_index]
        image = _render_page(page, 150)
        hints = _page_hints(page)
    finally:
        doc.close()

    if os.getenv("MOCK_VISION", "false").lower() == "true":
        # Smoke-test fallback: paragraphs only. Real deployments use the vision agent.
        blocks = []
        for hint in hints:
            blocks.append(
                {
                    "kind": "paragraph",
                    "column": "auto",
                    "translate": True,
                    "style": "normal",
                    "parts": [{"type": "text", "content": hint["text"]}],
                    "equation_latex": "",
                    "equation_number": "",
                    "bbox": hint["bbox"],
                }
            )
        return blocks

    hints_for_prompt = [
        {
            "id": h["id"],
            "bbox": h["bbox"],
            "text": h["text"],
            "font": h["font"],
            "font_size": h["font_size"],
        }
        for h in hints
    ]

    prompt = (
        "You are the page-reconstruction agent for a scientific PDF translation system.\n"
        f"This is source page {page_index + 1}. The attached PAGE IMAGE is authoritative.\n"
        "The extracted PDF text blocks below are hints for exact wording only; they may split "
        "equations badly, contain soft hyphens, or be in imperfect reading order.\n\n"
        "Reconstruct the page into SEMANTIC blocks in reading order so that a LaTeX renderer "
        "can recreate the publication as if the original author had written it in another language.\n\n"
        "Rules:\n"
        "1. DO NOT TRANSLATE. Copy natural-language source wording faithfully, repairing only "
        "PDF line-break hyphenation and obvious extraction artifacts.\n"
        "2. Every inline mathematical expression must be a part with type=math and its content "
        "must be VALID LaTeX without $ delimiters. Natural prose around it must be type=text.\n"
        "3. Every standalone/display equation must be kind=equation with equation_latex as "
        "VALID LaTeX. Never classify a mathematical equation as figure/table and never use an "
        "image for an equation.\n"
        "4. Keep equation numbers separately in equation_number, e.g. '12'. Do not put the "
        "number inside equation_latex.\n"
        "5. Real figures/tables may be figure/table blocks. bbox uses normalized page coordinates "
        "[x0,y0,x1,y1] from 0 to 1000 and should cover the visual object, not its caption.\n"
        "6. Preserve the document hierarchy: title, author, affiliation, abstract, section, "
        "subsection, paragraph, list_item, caption, reference, metadata, footer.\n"
        "7. For authors, affiliations, arXiv/journal metadata, emails, references and footer "
        "material set translate=false. For title/abstract/headings/body/captions set translate=true.\n"
        "8. Do not include page numbers as body content. A page-number-only item may be omitted.\n"
        "9. Respect the visible columns. In a two-column source, preserve left/right/full roles. "
        "Order full-width material first, then left-column top-to-bottom, then right-column "
        "top-to-bottom according to actual reading order.\n"
        "10. Do not omit prose or equations just because the page is dense.\n\n"
        "Global style already inferred from other page images:\n"
        + json.dumps(style, ensure_ascii=False)
        + "\n\nExtracted block hints:\n"
        + json.dumps(hints_for_prompt, ensure_ascii=False)
    )

    last_error: Exception | None = None
    for retry in range(2):
        try:
            result = _call_json(
                prompt
                + (
                    "\n\nIMPORTANT RETRY: the previous result was incomplete. "
                    "Cover every visible prose paragraph and every equation."
                    if retry
                    else ""
                ),
                [(image, "image/jpeg")],
                PAGE_SCHEMA,
                f"page-{page_index + 1}",
            )
            return _validate_page_result(result, hints, page_index + 1)
        except GeminiVisionError as exc:
            last_error = exc
            print(f"Page {page_index + 1} validation retry: {exc}", flush=True)

    raise GeminiVisionError(
        f"Could not faithfully reconstruct source page {page_index + 1}: {last_error}"
    )
