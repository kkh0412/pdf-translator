from __future__ import annotations

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
    models = [
        primary,
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
    ]
    out = []
    for m in models:
        if m and m not in out:
            out.append(m)
    return out


def _chunks(items: list[dict], max_items: int = 28, max_chars: int = 9000) -> Iterable[list[dict]]:
    batch, chars = [], 0
    for item in items:
        n = len(item.get("text", ""))
        if batch and (len(batch) >= max_items or chars + n > max_chars):
            yield batch
            batch, chars = [], 0
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
        "generation_config": {
            "thinking_level": "low",
        },
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=150) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GeminiHTTPError(exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TRANSIENT_GEMINI_ERROR: {exc}") from exc


def _translate_once(
    api_key: str,
    model: str,
    batch: list[dict],
    target_language: str,
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
        "Translate ordered semantic blocks from a scientific/academic document.\n"
        f"Target language: {LANGUAGE_NAMES[target_language]}.\n"
        "Write as if the ORIGINAL AUTHOR had written the publication directly in the target language: "
        "formal, natural, publication-quality academic prose.\n"
        "Use standard terminology of physics/mathematics/quantum information where applicable.\n"
        "Examples for Korean: coarse-graining -> 조대화, quantum state -> 양자 상태, "
        "relative entropy -> 상대 엔트로피, retrodiction -> 역추론 unless context strongly requires another standard term.\n"
        "Do not translate author names, citation numbers, equation numbers, URLs, DOIs, variable names, or acronyms.\n"
        "CRITICAL: tokens of the form [[MATH_0]], [[MATH_1]], ... are protected mathematical expressions. "
        "Preserve every such token EXACTLY, character-for-character. You may move a token to a grammatically "
        "natural position, but never edit, merge, duplicate, translate, or delete it.\n"
        "Do not add Markdown or commentary.\n"
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


def _request_batch(api_key: str, batch: list[dict], target_language: str) -> list[str]:
    last_error = None
    for model in _candidate_models():
        for attempt in range(2):
            try:
                print(
                    f"Translation agent: model={model}, attempt={attempt + 1}, "
                    f"blocks={len(batch)}",
                    flush=True,
                )
                return _translate_once(api_key, model, batch, target_language)
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


def _recover(api_key: str, batch: list[dict], target_language: str) -> list[str]:
    try:
        return _request_batch(api_key, batch, target_language)
    except Exception as exc:
        if len(batch) == 1:
            print(
                f"Translation fallback: preserving original block after repeated failure: {exc}",
                flush=True,
            )
            return [batch[0]["text"]]
        mid = len(batch) // 2
        return (
            _recover(api_key, batch[:mid], target_language)
            + _recover(api_key, batch[mid:], target_language)
        )


def translate_blocks(items: list[dict], target_language: str) -> dict[str, str]:
    if not items:
        return {}

    if os.getenv("MOCK_TRANSLATION", "false").lower() == "true":
        return {item["id"]: item["text"] for item in items}

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    if target_language not in LANGUAGE_NAMES:
        raise RuntimeError(f"Unsupported target language: {target_language}")

    result: dict[str, str] = {}
    for batch in _chunks(items):
        translated = _recover(api_key, batch, target_language)
        for item, value in zip(batch, translated):
            result[item["id"]] = value
    return result
