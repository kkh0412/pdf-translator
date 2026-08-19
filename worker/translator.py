from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Iterable


LANGUAGE_NAMES = {
    "ko": "Korean",
    "en": "English",
    "ja": "Japanese",
    "zh-CN": "Simplified Chinese",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
}

PLACEHOLDER_RE = re.compile(r"\[\[MATH_\d+\]\]")


class GeminiHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"Gemini API HTTP {status}: {detail}")


class GeminiOutputError(RuntimeError):
    pass


def _sanitize(value: str) -> str:
    value = unicodedata.normalize("NFC", value or "")
    out = []
    for ch in value:
        if ch in {"\n", "\t"}:
            out.append(ch)
        elif not unicodedata.category(ch).startswith("C"):
            out.append(ch)
    return "".join(out).strip()


def _candidate_models() -> list[str]:
    primary = os.getenv("GEMINI_TRANSLATION_MODEL", "gemini-3.6-flash").strip()
    models = [primary, "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    out = []
    for model in models:
        if model and model not in out:
            out.append(model)
    return out


def _chunks(
    items: list[dict],
    max_items: int = 30,
    max_chars: int = 10000,
) -> Iterable[list[dict]]:
    batch = []
    chars = 0
    for item in items:
        n = len(item.get("text", ""))
        if batch and (len(batch) >= max_items or chars + n > max_chars):
            yield batch
            batch = []
            chars = 0
        batch.append(item)
        chars += n
    if batch:
        yield batch


def _schema(count: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": count,
                "maxItems": count,
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }


def _extract_text(response: dict) -> str:
    chunks = []
    for step in response.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            if block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
    if chunks:
        return "".join(chunks)
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    raise GeminiOutputError("Gemini returned no text output")


def _call(api_key: str, model: str, prompt: str, count: int) -> dict:
    endpoint = "https://generativelanguage.googleapis.com/v1beta/interactions"
    body = {
        "model": model,
        "input": prompt,
        "store": False,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": _schema(count),
        },
        "generation_config": {"thinking_level": "low"},
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=150) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GeminiHTTPError(exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TRANSIENT_GEMINI_ERROR: {exc}") from exc


def _strategy_prompt(strategy: dict) -> str:
    glossary = strategy.get("terminology", [])
    glossary_lines = []
    for term in glossary:
        source = str(term.get("source", "")).strip()
        target = str(term.get("target", "")).strip()
        note = str(term.get("note", "")).strip()
        if source and target:
            glossary_lines.append(
                f"- {source} => {target}" + (f" ({note})" if note else "")
            )

    principles = "\n".join(
        f"- {item}" for item in strategy.get("translation_principles", [])
    )
    do_not = ", ".join(strategy.get("do_not_translate", []))

    return (
        f"Document field: {strategy.get('field', 'unknown')}\n"
        f"Subfield: {strategy.get('subfield', 'unknown')}\n"
        f"Document type: {strategy.get('document_type', 'academic document')}\n"
        f"Register: {strategy.get('register', 'formal academic')}\n"
        "MANDATORY TERMINOLOGY GLOSSARY:\n"
        + ("\n".join(glossary_lines) if glossary_lines else "- none supplied")
        + "\nTRANSLATION PRINCIPLES:\n"
        + (principles if principles else "- Use established specialist terminology.")
        + "\nDO NOT TRANSLATE / PRESERVE:\n"
        + (do_not if do_not else "author names, formulas, citations, URLs, DOIs")
    )


def _translate_once(
    api_key: str,
    model: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
) -> list[str]:
    compact = [
        {
            "index": i,
            "kind": item["kind"],
            "text": item["text"],
        }
        for i, item in enumerate(batch)
    ]

    prompt = (
        "Translate ordered semantic blocks from a scientific/academic publication.\n"
        f"Target language: {LANGUAGE_NAMES[target_language]}.\n"
        "The document was pre-scanned before translation. Follow the domain strategy and "
        "glossary below consistently throughout the paper.\n\n"
        + _strategy_prompt(strategy)
        + "\n\n"
        "Write as if the ORIGINAL AUTHOR had written the publication directly in the target "
        "language. Use polished specialist prose, not literal machine-translation phrasing.\n"
        "When a glossary entry applies, use its target term consistently unless the supplied "
        "note explicitly permits another rendering.\n"
        "For Korean: avoid mechanical parenthesized particles such as 은(는), 이(가), 을(를), "
        "와(과). Recast the sentence naturally. Do not create awkward hybrids such as "
        "'측도 M' when the quantum-mechanical meaning is '측정 M'.\n"
        "Do not translate author names, citation numbers, equation numbers, URLs, DOIs, "
        "variable names, or acronyms unless the strategy explicitly says otherwise.\n"
        "CRITICAL: [[MATH_0]], [[MATH_1]], ... are protected mathematical expressions. "
        "Preserve every placeholder EXACTLY, character-for-character. You may move it for "
        "natural grammar, but never edit, merge, duplicate, translate, or delete it.\n"
        "Do not add Markdown, commentary, or explanations.\n"
        f"Return exactly {len(batch)} strings in the same order.\n\n"
        + json.dumps(compact, ensure_ascii=False)
    )

    payload = _call(api_key, model, prompt, len(batch))
    try:
        parsed = json.loads(_extract_text(payload))
    except json.JSONDecodeError as exc:
        raise GeminiOutputError("Gemini returned invalid JSON") from exc

    values = parsed.get("translations")
    if not isinstance(values, list) or len(values) != len(batch):
        raise GeminiOutputError("Gemini returned the wrong number of translations")

    out = []
    for item, value in zip(batch, values):
        if not isinstance(value, str):
            raise GeminiOutputError("Gemini returned a non-string translation")
        value = _sanitize(value)
        if not value:
            raise GeminiOutputError("Gemini returned an empty translation")

        expected = sorted(PLACEHOLDER_RE.findall(item["text"]))
        actual = sorted(PLACEHOLDER_RE.findall(value))
        if expected != actual:
            raise GeminiOutputError(
                f"Math placeholders changed: expected={expected}, actual={actual}"
            )

        out.append(value)

    return out


def _request_batch(
    api_key: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
) -> list[str]:
    last_error = None

    for model in _candidate_models():
        for attempt in range(2):
            try:
                print(
                    f"Translation agent: model={model}, attempt={attempt + 1}, "
                    f"blocks={len(batch)}",
                    flush=True,
                )
                return _translate_once(
                    api_key,
                    model,
                    batch,
                    target_language,
                    strategy,
                )
            except GeminiOutputError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
                    continue
                break
            except GeminiHTTPError as exc:
                last_error = exc
                if exc.status == 404:
                    break
                if exc.status in {429, 500, 502, 503, 504}:
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    break
                raise

    raise RuntimeError(f"TRANSIENT_GEMINI_ERROR: {last_error}")


def _recover(
    api_key: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
) -> list[str]:
    try:
        return _request_batch(api_key, batch, target_language, strategy)
    except Exception as exc:
        if len(batch) == 1:
            print(
                "Translation fallback: preserving the original block after "
                f"repeated failure: {exc}",
                flush=True,
            )
            return [batch[0]["text"]]

        mid = len(batch) // 2
        return (
            _recover(api_key, batch[:mid], target_language, strategy)
            + _recover(api_key, batch[mid:], target_language, strategy)
        )


def translate_blocks(
    items: list[dict],
    target_language: str,
    strategy: dict,
) -> dict[str, str]:
    if not items:
        return {}

    if os.getenv("MOCK_TRANSLATION", "false").lower() == "true":
        return {item["id"]: item["text"] for item in items}

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    if target_language not in LANGUAGE_NAMES:
        raise RuntimeError(f"Unsupported target language: {target_language}")

    batches = list(_chunks(items))
    workers = max(1, min(2, int(os.getenv("TRANSLATION_WORKERS", "2"))))
    print(
        f"Translation plan: {len(batches)} batches, workers={workers}",
        flush=True,
    )

    translated_batches: list[list[str] | None] = [None] * len(batches)

    if workers == 1 or len(batches) <= 1:
        for index, batch in enumerate(batches):
            translated_batches[index] = _recover(
                api_key, batch, target_language, strategy
            )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_index = {
                executor.submit(
                    _recover,
                    api_key,
                    batch,
                    target_language,
                    strategy,
                ): index
                for index, batch in enumerate(batches)
            }
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                translated_batches[index] = future.result()

    result: dict[str, str] = {}
    for batch, translated in zip(batches, translated_batches):
        assert translated is not None
        for item, value in zip(batch, translated):
            result[item["id"]] = value

    return result
