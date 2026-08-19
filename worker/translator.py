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


class GeminiHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"Gemini API HTTP {status}: {detail}")


def _chunks(
    items: list[dict],
    max_items: int = 70,
    max_chars: int = 14000,
) -> Iterable[list[dict]]:
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


def _candidate_models() -> list[str]:
    # Translation / document processing favors the low-latency Flash-Lite model.
    primary = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    fallbacks = [
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
    ]
    out: list[str] = []
    for model in [primary, *fallbacks]:
        if model and model not in out:
            out.append(model)
    return out


def _extract_model_text(response: dict) -> str:
    chunks: list[str] = []
    for step in response.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            if block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
    if chunks:
        return "".join(chunks)

    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    raise RuntimeError("Gemini returned no model output text")


def _call_gemini(api_key: str, model: str, prompt: str) -> dict:
    endpoint = "https://generativelanguage.googleapis.com/v1beta/interactions"
    body = {
        "model": model,
        "input": prompt,
        "store": False,
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
        raise GeminiHTTPError(exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TRANSIENT_GEMINI_ERROR: connection error: {exc}") from exc


def _translate_batch(
    api_key: str,
    model: str,
    batch: list[dict],
    target_language: str,
) -> list[dict]:
    compact = [{"id": x["id"], "text": x["text"], "kind": x.get("kind", "paragraph")} for x in batch]
    prompt = (
        "You are translating text segments extracted from a PDF document.\n"
        f"Translate every item into {LANGUAGE_NAMES[target_language]}.\n"
        "Requirements:\n"
        "- Return exactly one translation for every input id.\n"
        "- Translate natural-language content faithfully and professionally.\n"
        "- Preserve citation markers, bracketed reference numbers, URLs, DOIs, "
        "acronyms, proper names, equations, symbols, and numeric values.\n"
        "- Do not add explanations, notes, Markdown, or commentary.\n"
        "- Preserve the document role indicated by kind (title, section, topic, bullet, verse, paragraph).\n"
        "- For kind=verse, preserve the original line breaks and line count as closely as Korean/target-language grammar allows.\n"
        "- For headings and bullets, translate only the wording; do not invent new numbering or bullet symbols.\n"
        "- Use natural professional book/document prose rather than terse UI language.\n"
        "- Do not omit meaning.\n\n"
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


def _is_transient_status(status: int) -> bool:
    return status in {429, 500, 502, 503, 504}


def translate_segments(items: list[dict], target_language: str) -> dict[str, str]:
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
    result: dict[str, str] = {}
    active_model = models[0]

    for batch_index, batch in enumerate(_chunks(items), start=1):
        parsed_items = None
        last_error: Exception | None = None
        ordered_models = [active_model] + [m for m in models if m != active_model]

        for model in ordered_models:
            for attempt in range(2):
                try:
                    print(
                        f"Gemini batch {batch_index}: model={model}, attempt={attempt + 1}",
                        flush=True,
                    )
                    parsed_items = _translate_batch(
                        api_key, model, batch, target_language
                    )
                    active_model = model
                    break
                except GeminiHTTPError as exc:
                    last_error = exc

                    # Model retired / inaccessible: immediately try the next model.
                    if exc.status == 404:
                        print(
                            f"{model} unavailable (404); trying fallback model.",
                            flush=True,
                        )
                        break

                    # Capacity/rate/backend errors: short retry, then another model.
                    if _is_transient_status(exc.status):
                        if attempt == 0:
                            delay = 2
                            print(
                                f"{model} temporary HTTP {exc.status}; "
                                f"retrying in {delay}s.",
                                flush=True,
                            )
                            time.sleep(delay)
                            continue
                        print(
                            f"{model} still unavailable (HTTP {exc.status}); "
                            "trying fallback model.",
                            flush=True,
                        )
                        break

                    raise
                except RuntimeError as exc:
                    last_error = exc
                    if str(exc).startswith("TRANSIENT_GEMINI_ERROR:"):
                        if attempt == 0:
                            time.sleep(2)
                            continue
                        break
                    raise

            if parsed_items is not None:
                break

        if parsed_items is None:
            raise RuntimeError(
                "TRANSIENT_GEMINI_ERROR: all Gemini fallback models are "
                f"temporarily unavailable. Last error: {last_error}"
            )

        for item in parsed_items:
            item_id = item.get("id")
            translation = item.get("translation")
            if isinstance(item_id, str) and isinstance(translation, str):
                result[item_id] = translation.strip()

    missing = [x["id"] for x in items if x["id"] not in result]
    if missing:
        raise RuntimeError(
            f"Gemini response missed {len(missing)} translation segments"
        )

    return result
