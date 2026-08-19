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



def _read_balanced_group(text: str, start: int) -> tuple[str, int]:
    """Read one {...} group and recursively normalize its mathematical body."""
    if start >= len(text) or text[start] != "{":
        return "", start

    depth = 0
    i = start
    while i < len(text):
        ch = text[i]

        if ch == "\\" and i + 1 < len(text) and text[i + 1] in "{}":
            i += 2
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                inner = text[start + 1:i]
                return "{" + _normalize_repeated_scripts(inner) + "}", i + 1
        i += 1

    # Leave malformed unmatched text untouched; preflight/source repair will
    # handle the genuine grouping error later.
    return text[start:], len(text)


def _read_tex_command(text: str, start: int) -> tuple[str, int]:
    """Read a TeX control sequence and its immediately attached brace arguments."""
    if start >= len(text) or text[start] != "\\":
        return "", start

    i = start + 1
    if i < len(text) and text[i].isalpha():
        while i < len(text) and text[i].isalpha():
            i += 1
    elif i < len(text):
        i += 1

    atom = text[start:i]

    # Commands such as \mathcal{M}, \widetilde{X}, \frac{a}{b} form one
    # syntactic atom for the purpose of subsequent scripts.
    while i < len(text) and text[i] == "{":
        group, i2 = _read_balanced_group(text, i)
        if i2 <= i:
            break
        atom += group
        i = i2

    return atom, i


def _read_script_argument(text: str, start: int) -> tuple[str, int]:
    """Read the single TeX token/group governed by _ or ^."""
    i = start
    while i < len(text) and text[i].isspace():
        i += 1

    if i >= len(text):
        return "", i

    if text[i] == "{":
        return _read_balanced_group(text, i)

    if text[i] == "\\":
        return _read_tex_command(text, i)

    return text[i], i + 1


def _read_math_atom(text: str, start: int) -> tuple[str, int]:
    """Read one base math atom before scripts are attached."""
    if start >= len(text):
        return "", start

    if text[start] == "{":
        return _read_balanced_group(text, start)

    if text[start] == "\\":
        return _read_tex_command(text, start)

    return text[start], start + 1


def _normalize_repeated_scripts(text: str) -> str:
    """Make repeated TeX sub/superscripts syntactically valid without deleting content.

    TeX forbids two subscripts or two superscripts on the same atom:
        C_A_B      -> Double subscript
        X^a^b      -> Double superscript

    Since the invalid input has no legal TeX interpretation, the least-invasive
    repair is to preserve token order and explicitly group the already-scripted
    atom before attaching the repeated script:
        C_A_B      -> {C_A}_B
        X_a^b_c    -> {X_a^b}_c

    Nested groups are processed recursively.
    """
    if not text:
        return text

    out: list[str] = []
    i = 0

    while i < len(text):
        ch = text[i]

        if ch.isspace():
            out.append(ch)
            i += 1
            continue

        # _ or ^ without a preceding atom is left for XeLaTeX/source repair.
        if ch in "_^":
            out.append(ch)
            i += 1
            continue

        atom, next_i = _read_math_atom(text, i)
        if next_i <= i:
            out.append(ch)
            i += 1
            continue

        current = atom
        i = next_i
        seen_scripts: set[str] = set()

        while True:
            j = i
            while j < len(text) and text[j].isspace():
                j += 1

            if j >= len(text) or text[j] not in "_^":
                break

            marker = text[j]
            arg, after = _read_script_argument(text, j + 1)
            if not arg:
                break

            if marker in seen_scripts:
                # Nest the previous scripted atom rather than dropping,
                # combining or reinterpreting either script.
                current = "{" + current + "}" + marker + arg
                seen_scripts = {marker}
            else:
                current += marker + arg
                seen_scripts.add(marker)

            i = after

        out.append(current)

    return "".join(out)


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

    # Generic TeX grammar repair: repeated _/_ or ^/^ scripts are illegal.
    # Preserve every token by nesting the already-scripted atom.
    latex = _normalize_repeated_scripts(latex)

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



def _clean_prose_text(text: str) -> str:
    text = unicodedata.normalize("NFC", str(text or ""))
    out: list[str] = []

    for ch in text:
        category = unicodedata.category(ch)
        if category.startswith("C"):
            if ch in {"\n", "\t", "\r"}:
                out.append(" ")
            continue
        out.append(ch)

    text = "".join(out)
    text = text.replace("\u00a0", " ").replace("\u00ad", "")
    text = re.sub(r"[ \t\r\n]+", " ", text)
    return text


def _read_leaked_math_wrapper(
    text: str,
    start: int,
) -> tuple[str, int] | None:
    r"""Read §math{...}, \math{...}, or $math{...} leaked into prose."""
    prefixes = ("§math{", "\\math{", "$math{")
    prefix = next((item for item in prefixes if text.startswith(item, start)), None)
    if prefix is None:
        return None

    open_index = start + len(prefix) - 1
    depth = 0
    i = open_index

    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1:i], i + 1
        i += 1

    return None


def _split_text_with_leaked_math(text: str) -> list[tuple[str, str]]:
    """Recover Vision pseudo-markup into real semantic text/math segments."""
    text = _clean_prose_text(text)
    result: list[tuple[str, str]] = []
    cursor = 0
    i = 0

    while i < len(text):
        parsed = _read_leaked_math_wrapper(text, i)
        if parsed is None:
            i += 1
            continue

        inner, end = parsed
        if i > cursor:
            result.append(("text", text[cursor:i]))

        while True:
            nested = _read_leaked_math_wrapper(inner, 0)
            if nested is None or nested[1] != len(inner):
                break
            inner = nested[0]

        result.append(("math", inner))
        cursor = end
        i = end

    if cursor < len(text):
        result.append(("text", text[cursor:]))

    if not result:
        result.append(("text", text))

    return result



_BARE_MATH_TRANSPORT_RE = re.compile(
    r"§(?:"
    r"math(?:cal|bf|rm|sf|tt)?|"
    r"boldsymbol|mathbf|mathrm|operatorname|text(?:sf|bf|it|tt|rm)?|"
    r"frac|sqrt|sum|prod|int|lim|log|ln|exp|"
    r"alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|"
    r"iota|kappa|lambda|mu|nu|xi|pi|varpi|rho|varrho|sigma|varsigma|"
    r"tau|upsilon|phi|varphi|chi|psi|omega|"
    r"Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Upsilon|Phi|Psi|Omega|"
    r"otimes|oplus|Vert|vert|lvert|rvert|langle|rangle|"
    r"dagger|infty|in|notin|leq|geq|neq|to|mapsto|cap|cup|"
    r"widehat|widetilde|hat|tilde|bar|overline|underbrace|overbrace|"
    r"ket|bra|braket|mel"
    r")(?:\b|\s|\{|\[|\()"
)


def _contains_bare_math_transport(text: str) -> bool:
    """Return true only for § sequences that look like LaTeX commands.

    A literal section sign or an OCR-confused Unicode name must not be rejected
    merely because it contains the § character.
    """
    return bool(_BARE_MATH_TRANSPORT_RE.search(str(text or "")))



_MATH_TRANSPORT_COMMAND = re.compile(
    r"§(?:"
    r"math(?:cal|bf|rm|sf|tt)?|"
    r"boldsymbol|mathbf|mathrm|operatorname|text(?:sf|bf|it|tt|rm)?|"
    r"frac|dfrac|tfrac|sqrt|sum|prod|int|iint|iiint|oint|lim|log|ln|exp|"
    r"alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|"
    r"iota|kappa|lambda|mu|nu|xi|pi|varpi|rho|varrho|sigma|varsigma|"
    r"tau|upsilon|phi|varphi|chi|psi|omega|"
    r"Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Upsilon|Phi|Psi|Omega|"
    r"otimes|oplus|ominus|odot|times|cdot|pm|mp|"
    r"Vert|vert|lVert|rVert|lvert|rvert|langle|rangle|left|right|"
    r"dagger|ddagger|star|ast|infty|in|notin|subset|subseteq|supset|supseteq|"
    r"leq|geq|neq|approx|sim|simeq|cong|propto|to|mapsto|"
    r"cap|cup|wedge|vee|forall|exists|partial|nabla|"
    r"widehat|widetilde|hat|tilde|bar|overline|underline|"
    r"underbrace|overbrace|"
    r"ket|bra|braket|mel|Tr|rank|supp|diag|coloneq|coloneqq"
    r")(?=$|[^A-Za-z])"
)


def _is_transport_command_at(text: str, index: int) -> bool:
    return bool(_MATH_TRANSPORT_COMMAND.match(text, index))


def _find_transport_commands(text: str) -> list[re.Match]:
    return list(_MATH_TRANSPORT_COMMAND.finditer(str(text or "")))


def _balanced_group_end(
    text: str,
    index: int,
    open_char: str,
    close_char: str,
) -> int:
    if index >= len(text) or text[index] != open_char:
        return index

    depth = 0
    i = index
    while i < len(text):
        ch = text[i]
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1

    return index


def _consume_transport_command(text: str, start: int) -> int:
    match = _MATH_TRANSPORT_COMMAND.match(text, start)
    if not match:
        return start

    i = match.end()
    command = match.group(0)

    # Vision may emit formatting commands in either §mathcal{M} or §mathcal M form.
    if command in {
        "§mathcal", "§mathbf", "§mathrm", "§mathsf", "§mathtt",
        "§boldsymbol", "§hat", "§tilde", "§bar", "§widehat",
        "§widetilde", "§overline", "§underline",
    }:
        j = i
        while j < len(text) and text[j] == " ":
            j += 1

        if j < len(text):
            if text[j] == "{":
                after = _balanced_group_end(text, j, "{", "}")
                if after > j:
                    i = after
            elif text[j].isalnum():
                i = j + 1

    # Explicit brace arguments such as §frac{a}{b}, §sqrt{x}, §text{...}.
    while i < len(text) and text[i] == "{":
        after = _balanced_group_end(text, i, "{", "}")
        if after <= i:
            break
        i = after

    # Direct scripts.
    while i < len(text) and text[i] in "_^":
        i += 1

        if i < len(text) and text[i] == "{":
            after = _balanced_group_end(text, i, "{", "}")
            if after <= i:
                break
            i = after
        elif i < len(text) and text[i] == "§":
            after = _consume_transport_command(text, i)
            i = after if after > i else i + 1
        elif i < len(text):
            i += 1

    return i


_MATHISH_CONTIGUOUS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "_^{}()[]|=+-*/,:.<>"
)


def _expand_math_transport_span(
    text: str,
    command_start: int,
    command_end: int,
) -> tuple[int, int]:
    """Recover the maximal contiguous inline formula containing §command."""
    left = command_start
    right = command_end

    while left > 0 and text[left - 1] in _MATHISH_CONTIGUOUS:
        left -= 1

    while right < len(text):
        if text[right] == "§" and _is_transport_command_at(text, right):
            after = _consume_transport_command(text, right)
            if after > right:
                right = after
                continue

        if text[right] in _MATHISH_CONTIGUOUS:
            right += 1
            continue

        break

    # If the initial command sits inside C_{§mathcal{M}} or D(§rho...), absorb
    # immediately adjacent closing delimiters until the local token is balanced.
    pairs = (("{", "}"), ("(", ")"), ("[", "]"))
    for open_char, close_char in pairs:
        segment = text[left:right]
        opens = segment.count(open_char)
        closes = segment.count(close_char)

        while closes < opens and right < len(text) and text[right] == close_char:
            right += 1
            closes += 1

        while opens < closes and left > 0 and text[left - 1] == open_char:
            left -= 1
            opens += 1

    return left, right


def _merge_overlapping_spans(
    spans: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if not spans:
        return []

    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    return [(item[0], item[1]) for item in merged]


def _trim_terminal_prose_punctuation(
    text: str,
    start: int,
    end: int,
) -> tuple[int, int]:
    """Keep sentence punctuation outside the inline math placeholder.

    Internal punctuation stays untouched. Thus:
      §gamma,                       -> math §gamma + prose comma
      D(§rho§Vert§gamma).           -> math D(...) + prose period
      S_{§mathcal M,§gamma}^{(j)}   -> internal comma remains math
    """
    while end > start and text[end - 1] in ",.;:":
        end -= 1
    return start, end


def _split_text_with_bare_transport(
    text: str,
) -> list[tuple[str, str]]:
    """Convert only recognized § LaTeX transport syntax into math segments."""
    text = _clean_prose_text(text)
    matches = _find_transport_commands(text)

    if not matches:
        return [("text", text)]

    spans: list[tuple[int, int]] = []

    for match in matches:
        consumed = _consume_transport_command(text, match.start())
        start, end = _expand_math_transport_span(
            text,
            match.start(),
            max(match.end(), consumed),
        )
        spans.append((start, end))

    spans = _merge_overlapping_spans(spans)
    spans = [
        _trim_terminal_prose_punctuation(text, start, end)
        for start, end in spans
    ]

    result: list[tuple[str, str]] = []
    cursor = 0

    for start, end in spans:
        if end <= start:
            continue

        if start > cursor:
            result.append(("text", text[cursor:start]))

        formula = text[start:end].strip()
        if formula:
            result.append(("math", formula))

        cursor = end

    if cursor < len(text):
        result.append(("text", text[cursor:]))

    return result or [("text", text)]


def _recover_all_math_from_text(
    text: str,
) -> list[tuple[str, str]]:
    """Recover §math{...} wrappers, then bare transport in remaining prose."""
    recovered: list[tuple[str, str]] = []

    for part_type, content in _split_text_with_leaked_math(text):
        if part_type == "math":
            recovered.append(("math", content))
        else:
            recovered.extend(_split_text_with_bare_transport(content))

    return recovered


def _assemble_source(block: dict) -> tuple[str, dict[str, str]]:
    pieces: list[str] = []
    math_map: dict[str, str] = {}
    math_index = 0

    def append_math(content: str) -> None:
        nonlocal math_index

        cleaned = _clean_math(content)
        if not cleaned:
            return

        token = f"[[MATH_{math_index}]]"
        math_map[token] = cleaned
        pieces.append(token)
        math_index += 1

    for part in block.get("parts", []):
        part_type = part.get("type")
        content = str(part.get("content", ""))

        if part_type == "math":
            append_math(content)
            continue

        for recovered_type, recovered_content in _recover_all_math_from_text(content):
            if recovered_type == "math":
                append_math(recovered_content)
            else:
                pieces.append(_clean_prose_text(recovered_content))

    source = "".join(pieces).strip()
    source = re.sub(r"[ \t\r\n]+", " ", source)

    if _contains_bare_math_transport(source):
        raise RuntimeError(
            "Unrecoverable JSON-safe math transport remained in prose after "
            f"automatic inline-math recovery: {source[:240]}"
        )

    return source, math_map


def _render_translated_text(text: str, math_map: dict[str, str]) -> str:
    text = _clean_prose_text(text)

    if _contains_bare_math_transport(text) or "\\math{" in text:
        raise RuntimeError(
            "Unprotected math transport marker reached final text rendering"
        )

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


def _normalized_bbox_to_rect(
    page: pymupdf.Page,
    bbox_norm: list[float],
) -> pymupdf.Rect:
    x0, y0, x1, y1 = bbox_norm
    return (
        pymupdf.Rect(
            page.rect.width * x0 / 1000.0,
            page.rect.height * y0 / 1000.0,
            page.rect.width * x1 / 1000.0,
            page.rect.height * y1 / 1000.0,
        )
        & page.rect
    )


def _rect_intersection_area(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = a & b
    if inter.is_empty or inter.width <= 0 or inter.height <= 0:
        return 0.0
    return float(inter.width * inter.height)


def _raster_candidates(
    doc: pymupdf.Document,
    page: pymupdf.Page,
    rect: pymupdf.Rect,
) -> list[dict]:
    candidates: list[dict] = []
    visual_area = max(1.0, float(rect.width * rect.height))
    seen: set[tuple] = set()

    for image_info in page.get_images(full=True):
        xref = int(image_info[0])
        try:
            placements = page.get_image_rects(xref)
            extracted = doc.extract_image(xref)
        except Exception:
            continue

        for placement in placements:
            placement = pymupdf.Rect(placement)
            key = (
                xref,
                round(placement.x0, 3),
                round(placement.y0, 3),
                round(placement.x1, 3),
                round(placement.y1, 3),
            )
            if key in seen:
                continue
            seen.add(key)

            intersection = _rect_intersection_area(rect, placement)
            if intersection <= 0:
                continue

            placement_area = max(
                1.0,
                float(placement.width * placement.height),
            )
            candidates.append(
                {
                    "xref": xref,
                    "rect": placement,
                    "coverage": intersection / visual_area,
                    "image_coverage": intersection / placement_area,
                    "width": int(extracted.get("width", 0) or 0),
                    "height": int(extracted.get("height", 0) or 0),
                    "ext": str(extracted.get("ext", "") or "").lower(),
                    "image": extracted.get("image", b""),
                }
            )

    candidates.sort(
        key=lambda item: (item["coverage"], item["image_coverage"]),
        reverse=True,
    )
    return candidates


def _source_text_chars_in_rect(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
) -> int:
    text = page.get_text("text", clip=rect) or ""
    return sum(1 for char in text if char.isalnum())


def _vector_drawing_count_in_rect(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
) -> int:
    try:
        drawings = page.get_drawings()
    except Exception:
        return 0

    count = 0
    for drawing in drawings:
        drect = drawing.get("rect")
        if drect is None:
            continue
        if _rect_intersection_area(pymupdf.Rect(drect), rect) > 0.5:
            count += 1
    return count


def _classify_figure_source(
    doc: pymupdf.Document,
    page: pymupdf.Page,
    rect: pymupdf.Rect,
) -> tuple[str, dict | None]:
    """Choose raster only for a genuinely bitmap-only visual.

    Any meaningful PDF text/vector drawing, or raster+vector mixture, is kept
    in a clipped PDF asset so vector objects remain vector.
    """
    candidates = _raster_candidates(doc, page, rect)
    top = candidates[0] if candidates else None
    text_chars = _source_text_chars_in_rect(page, rect)
    drawing_count = _vector_drawing_count_in_rect(page, rect)

    if (
        top is not None
        and top["coverage"] >= 0.86
        and text_chars <= 3
        and drawing_count <= 1
    ):
        return "raster", top

    return "vector_or_mixed", top


def _save_raster_asset(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    candidate: dict,
    assets_dir: Path,
    index: int,
) -> Path:
    placement = pymupdf.Rect(candidate["rect"])
    requested_area = max(1.0, float(rect.width * rect.height))
    intersection = _rect_intersection_area(rect, placement)

    almost_exact = (
        intersection / requested_area >= 0.97
        and intersection
        / max(1.0, float(placement.width * placement.height))
        >= 0.97
    )

    ext = str(candidate.get("ext", "") or "").lower()
    raw = candidate.get("image", b"")

    # Exact embedded JPEG/PNG: retain original bytes and compression.
    if almost_exact and raw and ext in {"png", "jpg", "jpeg"}:
        suffix = ".jpg" if ext in {"jpg", "jpeg"} else ".png"
        out = assets_dir / f"figure_{index:04d}_raster{suffix}"
        out.write_bytes(raw)
        return out

    # A crop of a bitmap remains bitmap. Render close to the source image's
    # native displayed sampling density and save losslessly as PNG.
    source_width = max(1, int(candidate.get("width", 0) or 0))
    source_height = max(1, int(candidate.get("height", 0) or 0))
    scale_x = source_width / max(1.0, placement.width)
    scale_y = source_height / max(1.0, placement.height)
    scale = max(1.0, min(8.0, max(scale_x, scale_y)))

    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale),
        clip=rect,
        alpha=False,
    )
    out = assets_dir / f"figure_{index:04d}_raster.png"
    pix.save(out)
    return out


def _save_vector_asset(
    doc: pymupdf.Document,
    page_index: int,
    rect: pymupdf.Rect,
    assets_dir: Path,
    index: int,
) -> Path:
    """Clip the source PDF without rasterizing paths, PDF text or mixed media."""
    out = assets_dir / f"figure_{index:04d}_vector.pdf"
    clipped = pymupdf.open()
    try:
        target = clipped.new_page(width=rect.width, height=rect.height)
        target.show_pdf_page(
            target.rect,
            doc,
            page_index,
            clip=rect,
            keep_proportion=False,
        )
        clipped.save(
            out,
            garbage=4,
            deflate=True,
            clean=True,
        )
    finally:
        clipped.close()
    return out


def _extract_figure_asset(
    doc: pymupdf.Document,
    page_index: int,
    bbox_norm: list[float],
    assets_dir: Path,
    index: int,
) -> tuple[Path, str]:
    page = doc[page_index]
    rect = _normalized_bbox_to_rect(page, bbox_norm)

    if rect.width < 4 or rect.height < 4:
        raise RuntimeError(
            f"Vision agent produced an invalid figure bbox: {bbox_norm}"
        )

    source_type, candidate = _classify_figure_source(doc, page, rect)

    if source_type == "raster" and candidate is not None:
        asset = _save_raster_asset(
            page,
            rect,
            candidate,
            assets_dir,
            index,
        )
        print(
            f"Figure asset {index}: bitmap source preserved as {asset.name}",
            flush=True,
        )
        return asset, "raster"

    asset = _save_vector_asset(
        doc,
        page_index,
        rect,
        assets_dir,
        index,
    )
    print(
        f"Figure asset {index}: vector/mixed source preserved as {asset.name}",
        flush=True,
    )
    return asset, "vector_or_mixed"


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
                f"PDF는 최대 {max_pages}페이지까지 번역할 수 있습니다."
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

                if block["kind"] == "figure":
                    asset_path, asset_type = _extract_figure_asset(
                        doc,
                        pno,
                        block["bbox"],
                        assets_dir,
                        asset_index,
                    )
                    block["asset"] = asset_path
                    block["asset_type"] = asset_type
                    asset_index += 1
                    all_blocks.append(block)
                    continue

                if block["kind"] == "table":
                    semantic_rows = []
                    for row_index, row in enumerate(block.get("table_rows") or []):
                        semantic_cells = []
                        for cell_index, original_cell in enumerate(row.get("cells") or []):
                            cell = dict(original_cell)
                            source_text, math_map = _assemble_source(
                                {"parts": cell.get("parts", [])}
                            )
                            translation_id = (
                                f"{block_id}__r{row_index}c{cell_index}"
                            )
                            cell["source_text"] = source_text
                            cell["math_map"] = math_map
                            cell["translation_id"] = translation_id
                            semantic_cells.append(cell)

                            if block.get("translate") and source_text:
                                translation_items.append(
                                    {
                                        "id": translation_id,
                                        "kind": "table_cell",
                                        "text": source_text,
                                    }
                                )

                        semantic_rows.append({"cells": semantic_cells})

                    block["table_rows"] = semantic_rows
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



def _table_column_count(block: dict) -> int:
    return max(
        (
            sum(
                max(1, int(cell.get("colspan", 1)))
                for cell in row.get("cells", [])
            )
            for row in block.get("table_rows", [])
        ),
        default=0,
    )


def _table_column_type(alignment: str) -> str:
    return {
        "left": "L",
        "center": "C",
        "right": "R",
    }.get(str(alignment or "").lower(), "L")


def _table_multicolumn_type(alignment: str) -> str:
    return {
        "left": "l",
        "center": "c",
        "right": "r",
    }.get(str(alignment or "").lower(), "l")


def _render_table_tex(
    block: dict,
    translations: dict[str, str],
) -> str:
    """Convert the semantic table block into actual LaTeX table syntax."""
    rows = block.get("table_rows") or []
    column_count = _table_column_count(block)
    if not rows or column_count <= 0:
        return ""

    alignments = list(block.get("table_alignments") or [])
    if len(alignments) != column_count:
        alignments = ["left"] * column_count

    column_spec = "".join(
        _table_column_type(alignment)
        for alignment in alignments
    )

    header_rows = max(
        0,
        min(int(block.get("table_header_rows", 0) or 0), len(rows)),
    )

    output = [
        r"\begin{center}",
        r"\begingroup",
        r"\small",
        r"\setlength{\tabcolsep}{3.8pt}",
        r"\renewcommand{\arraystretch}{1.12}",
        rf"\begin{{tabularx}}{{\linewidth}}{{@{{}}{column_spec}@{{}}}}",
        r"\toprule",
    ]

    for row_index, row in enumerate(rows):
        cells_tex: list[str] = []
        logical_column = 0

        for cell in row.get("cells", []):
            source = cell.get("source_text", "")
            translation_id = cell.get("translation_id", "")
            translated = translations.get(translation_id, source)

            rendered = _render_translated_text(
                translated,
                cell.get("math_map", {}),
            )
            rendered = _style_wrap(
                cell.get("style", "normal"),
                rendered,
            )

            colspan = max(1, int(cell.get("colspan", 1)))
            alignment = cell.get("align") or (
                alignments[logical_column]
                if logical_column < len(alignments)
                else "left"
            )

            if colspan > 1:
                rendered = (
                    rf"\multicolumn{{{colspan}}}"
                    rf"{{{_table_multicolumn_type(alignment)}}}"
                    rf"{{{rendered}}}"
                )

            cells_tex.append(rendered)
            logical_column += colspan

        output.append(" & ".join(cells_tex) + r" \\")
        if header_rows and row_index + 1 == header_rows:
            output.append(r"\midrule")

    output.extend(
        [
            r"\bottomrule",
            r"\end{tabularx}",
            r"\endgroup",
            r"\end{center}",
        ]
    )
    return "\n".join(output) + "\n"


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
    section_weight_cmd = (
        r"\bfseries"
        if style.get("section_weight", "normal") == "bold"
        else ""
    )

    preamble = rf"""\documentclass[10pt]{{article}}
\usepackage{{fontspec}}
\usepackage{{xetexko}}
\usepackage{{geometry}}
\usepackage{{graphicx}}
\usepackage{{xcolor}}
\usepackage{{amsmath,amssymb}}
\usepackage{{enumitem}}
\usepackage{{multicol}}
\usepackage{{array}}
\usepackage{{tabularx}}
\usepackage{{booktabs}}
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
\newcolumntype{{L}}{{>{{\raggedright\arraybackslash}}X}}
\newcolumntype{{C}}{{>{{\centering\arraybackslash}}X}}
\newcolumntype{{R}}{{>{{\raggedleft\arraybackslash}}X}}
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
  {{\sffamily {section_weight_cmd}\fontsize{{{section_size:.2f}pt}}{{{section_size*1.18:.2f}pt}}\selectfont #1}}
  \par\vspace{{0.25em}}}}
\newcommand{{\SourceSubsection}}[1]{{%
  \par\vspace{{0.40em}}\noindent
  {{\sffamily {section_weight_cmd} #1}}\par\vspace{{0.16em}}}}
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

        if kind == "figure":
            out.append(render_nonfloat_visual(block))
            continue

        if kind == "table":
            out.append(_render_table_tex(block, translations))
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

    integrity_errors: list[str] = []
    for item in translation_items:
        translated = translations.get(item["id"], "")
        if not translated:
            integrity_errors.append(f"{item['id']}: empty translation")
            continue
        if _contains_bare_math_transport(translated) or "\\math{" in translated:
            integrity_errors.append(
                f"{item['id']}: internal math transport marker"
            )

    if integrity_errors:
        raise RuntimeError(
            "Translation integrity check failed before PDF rendering: "
            + "; ".join(integrity_errors[:8])
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
