import json
import os
import time
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


TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "translation": {"type": "string"},
                },
                "required": ["id", "translation"],
            },
        }
    },
    "required": ["items"],
}


def _chunks(items: list[dict], max_items: int = 80, max_chars: int = 16000) -> Iterable[list[dict]]:
    """Use fairly large batches to reduce Gemini API round trips."""
    batch: list[dict] = []
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


def _is_retryable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "429",
        "resource_exhausted",
        "rate limit",
        "rate_limit",
        "503",
        "unavailable",
        "temporarily unavailable",
        "deadline exceeded",
        "timed out",
    )
    return any(marker in text for marker in markers)


def _is_model_unavailable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "404" in text
        and (
            "not_found" in text
            or "no longer available" in text
            or "model" in text
        )
    )


def _candidate_models() -> list[str]:
    primary = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip()
    fallbacks = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
    models: list[str] = []
    for model in [primary, *fallbacks]:
        if model and model not in models:
            models.append(model)
    return models


def _extract_model_text(response: dict) -> str:
    # Interactions API REST responses expose model output as timeline steps.
    chunks: list[str] = []
    for step in response.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            if block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])

    if chunks:
        return "".join(chunks)

    # Defensive fallback in case the API adds a convenience field.
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    raise RuntimeError("Gemini returned no model output text")


def _call_gemini(api_key: str, model: str, prompt: str) -> dict:
    endpoint = "https://generativelanguage.googleapis.com/v1beta/interactions"
    body = {
        "model": model,
        "input": prompt,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": TRANSLATION_SCHEMA,
        },
        "generation_config": {
            "thinking_level": "low",
        },
    }

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
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini API connection error: {exc}") from exc


def _translate_batch(api_key: str, model: str, batch: list[dict], target_language: str) -> list[dict]:
    compact = [{"id": x["id"], "text": x["text"]} for x in batch]
    prompt = (
        "You are translating text segments extracted from a PDF document.\n"
        f"Translate every item into {LANGUAGE_NAMES[target_language]}.\n"
        "Requirements:\n"
        "- Return exactly one translation for every input id.\n"
        "- Translate natural-language content faithfully and professionally.\n"
        "- Preserve citation markers, bracketed reference numbers, URLs, DOIs, acronyms, proper names, equations, symbols, and numeric values.\n"
        "- Do not add explanations, notes, Markdown, or commentary.\n"
        "- Prefer compact wording because each translation must fit inside the original PDF text box.\n"
        "- Do not omit meaning merely to shorten the translation.\n\n"
        "INPUT JSON:\n"
        + json.dumps(compact, ensure_ascii=False)
    )

    response = _call_gemini(api_key, model, prompt)
    output_text = _extract_model_text(response)

    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned invalid structured JSON") from exc

    items = parsed.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Gemini structured response is missing items")

    return items


def translate_segments(items: list[dict], target_language: str) -> dict[str, str]:
    """Translate text segments with Gemini while keeping segment IDs stable."""
    if not items:
        return {}

    if os.getenv("MOCK_TRANSLATION", "false").lower() == "true":
        return {
            item["id"]: f"[{LANGUAGE_NAMES.get(target_language, target_language)}] {item['text']}"
            for item in items
        }

    if target_language not in LANGUAGE_NAMES:
        raise ValueError("Unsupported target language")

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    models = _candidate_models()
    active_model = models[0]
    result: dict[str, str] = {}

    for batch in _chunks(items):
        parsed_items = None
        last_error = None

        # Prefer the newest model, but automatically fall back if Google
        # retires it or the current API key cannot access it. Once a model
        # succeeds, reuse it for the rest of the document.
        ordered_models = [active_model] + [m for m in models if m != active_model]
        for model in ordered_models:
            model_unavailable = False
            for attempt in range(4):
                try:
                    parsed_items = _translate_batch(api_key, model, batch, target_language)
                    active_model = model
                    break
                except Exception as exc:
                    last_error = exc
                    if _is_model_unavailable_error(exc):
                        model_unavailable = True
                        break
                    if attempt >= 3 or not _is_retryable_error(exc):
                        raise
                    time.sleep(2 ** attempt)

            if parsed_items is not None:
                break
            if model_unavailable:
                continue

        if parsed_items is None:
            raise RuntimeError(f"Gemini translation failed on all configured models: {last_error}")

        for item in parsed_items:
            item_id = item.get("id")
            translation = item.get("translation")
            if isinstance(item_id, str) and isinstance(translation, str):
                result[item_id] = translation.strip()

    missing = [x["id"] for x in items if x["id"] not in result]
    if missing:
        raise RuntimeError(f"Gemini response missed {len(missing)} segments")

    return result
