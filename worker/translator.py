import json
import os
import time
import urllib.error
import urllib.request
import unicodedata
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


class GeminiHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"Gemini API HTTP {status}: {detail}")


class GeminiOutputError(RuntimeError):
    """Gemini returned JSON, but it could not be safely mapped to the input batch."""


def _sanitize_translation_text(value: str) -> str:
    """Normalize model output and remove invisible control bytes unsafe for XeTeX."""
    value = unicodedata.normalize("NFC", value or "")
    out: list[str] = []
    for ch in value:
        if ch in {"\n", "\t"}:
            out.append(ch)
            continue
        if unicodedata.category(ch).startswith("C"):
            continue
        if ch == "\u0338":
            continue
        out.append(ch)
    return "".join(out).strip()


def _chunks(
    items: list[dict],
    max_items: int = 40,
    max_chars: int = 9000,
) -> Iterable[list[dict]]:
    """Keep batches modest so long documents do not encourage omitted entries."""
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


def _translation_schema(count: int) -> dict:
    # Exact-length arrays are supported by Gemini structured output.
    # The application owns the input IDs, so the model never has to reproduce them.
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "description": (
                    "Translations in exactly the same order as the input segments. "
                    f"The array must contain exactly {count} strings."
                ),
                "items": {"type": "string"},
                "minItems": count,
                "maxItems": count,
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }


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

    raise GeminiOutputError("Gemini returned no model output text")


def _call_gemini(api_key: str, model: str, prompt: str, count: int) -> dict:
    endpoint = "https://generativelanguage.googleapis.com/v1beta/interactions"
    body = {
        "model": model,
        "input": prompt,
        "store": False,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": _translation_schema(count),
        },
        "generation_config": {
            "thinking_level": "low",
            "temperature": 0.1,
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


def _translate_batch_once(
    api_key: str,
    model: str,
    batch: list[dict],
    target_language: str,
) -> list[str]:
    # index is only for human/model orientation. IDs stay entirely local.
    compact = [
        {
            "index": i,
            "kind": item.get("kind", "paragraph"),
            "text": item["text"],
        }
        for i, item in enumerate(batch)
    ]

    prompt = (
        "You are translating ordered text segments extracted from a PDF document.\n"
        f"Translate every segment into {LANGUAGE_NAMES[target_language]}.\n"
        f"There are exactly {len(batch)} input segments.\n"
        "Return exactly the same number of translations in the same order.\n"
        "Do not merge, skip, split, reorder, or duplicate segments.\n"
        "Requirements:\n"
        "- Translate natural-language content faithfully and professionally.\n"
        "- Preserve citation markers, bracketed reference numbers, URLs, DOIs, "
        "acronyms, proper names, equations, symbols, and numeric values.\n"
        "- Do not add explanations, notes, Markdown, or commentary.\n"
        "- Preserve the document role indicated by kind (title, section, topic, bullet, verse, paragraph).\n"
        "- For kind=verse, preserve original line breaks and line count as closely as grammar allows.\n"
        "- For headings and bullets, translate only the wording; do not invent numbering or bullet symbols.\n"
        "- Use natural professional book/document prose rather than terse UI language.\n"
        "- Do not omit meaning.\n\n"
        "INPUT JSON ARRAY (ordered):\n"
        + json.dumps(compact, ensure_ascii=False)
    )

    response = _call_gemini(api_key, model, prompt, len(batch))
    output_text = _extract_model_text(response)

    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise GeminiOutputError("Gemini returned invalid structured JSON") from exc

    translations = parsed.get("translations")
    if not isinstance(translations, list):
        raise GeminiOutputError("Gemini structured response is missing translations")

    if len(translations) != len(batch):
        raise GeminiOutputError(
            f"Gemini returned {len(translations)} translations for {len(batch)} segments"
        )

    cleaned: list[str] = []
    for i, value in enumerate(translations):
        if not isinstance(value, str):
            raise GeminiOutputError(f"Gemini returned a non-string translation at index {i}")
        sanitized = _sanitize_translation_text(value)
        if not sanitized:
            raise GeminiOutputError(f"Gemini returned an empty translation at index {i}")
        cleaned.append(sanitized)

    return cleaned


def _is_transient_status(status: int) -> bool:
    return status in {429, 500, 502, 503, 504}


def _request_with_model_fallback(
    api_key: str,
    batch: list[dict],
    target_language: str,
    models: list[str],
    preferred_model: str,
    label: str,
) -> tuple[list[str], str]:
    ordered_models = [preferred_model] + [m for m in models if m != preferred_model]
    last_error: Exception | None = None

    for model in ordered_models:
        # Retry malformed/semantically invalid output once on the same model.
        for attempt in range(2):
            try:
                print(
                    f"Gemini {label}: model={model}, attempt={attempt + 1}, "
                    f"segments={len(batch)}",
                    flush=True,
                )
                translations = _translate_batch_once(
                    api_key, model, batch, target_language
                )
                return translations, model

            except GeminiOutputError as exc:
                last_error = exc
                if attempt == 0:
                    print(
                        f"Gemini {label}: output validation failed ({exc}); retrying once.",
                        flush=True,
                    )
                    time.sleep(1)
                    continue
                # Bad output twice: splitting the batch is more useful than trying
                # the same large request across every model.
                raise

            except GeminiHTTPError as exc:
                last_error = exc
                if exc.status == 404:
                    print(
                        f"{model} unavailable (404); trying fallback model.",
                        flush=True,
                    )
                    break
                if _is_transient_status(exc.status):
                    if attempt == 0:
                        delay = 2
                        print(
                            f"{model} temporary HTTP {exc.status}; retrying in {delay}s.",
                            flush=True,
                        )
                        time.sleep(delay)
                        continue
                    print(
                        f"{model} still unavailable (HTTP {exc.status}); trying fallback model.",
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

    raise RuntimeError(
        "TRANSIENT_GEMINI_ERROR: all Gemini fallback models are temporarily "
        f"unavailable. Last error: {last_error}"
    )


def _translate_resilient(
    api_key: str,
    batch: list[dict],
    target_language: str,
    models: list[str],
    preferred_model: str,
    label: str,
    depth: int = 0,
) -> tuple[list[str], str]:
    """Translate a batch; if structured output is incomplete, retry only that batch.

    Large malformed batches are split recursively. A single irrecoverable segment is
    preserved in the source language rather than making the entire PDF disappear.
    """
    try:
        return _request_with_model_fallback(
            api_key,
            batch,
            target_language,
            models,
            preferred_model,
            label,
        )
    except GeminiOutputError as exc:
        print(
            f"Gemini {label}: incomplete/invalid structured output after retry: {exc}",
            flush=True,
        )

        if len(batch) == 1:
            # Preserve document completeness. This is deliberately visible in logs,
            # but the PDF keeps the source text instead of dropping the segment.
            print(
                f"WARNING: preserving source text for one unrecoverable segment "
                f"({batch[0]['id']}).",
                flush=True,
            )
            return [batch[0]["text"]], preferred_model

        mid = len(batch) // 2
        left = batch[:mid]
        right = batch[mid:]
        print(
            f"Gemini {label}: splitting {len(batch)} segments into "
            f"{len(left)} + {len(right)} for targeted recovery.",
            flush=True,
        )

        left_values, active_model = _translate_resilient(
            api_key,
            left,
            target_language,
            models,
            preferred_model,
            f"{label}.L",
            depth + 1,
        )
        right_values, active_model = _translate_resilient(
            api_key,
            right,
            target_language,
            models,
            active_model,
            f"{label}.R",
            depth + 1,
        )
        return left_values + right_values, active_model


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
        translations, active_model = _translate_resilient(
            api_key,
            batch,
            target_language,
            models,
            active_model,
            f"batch {batch_index}",
        )

        # Mapping is deterministic: model never generates or edits IDs.
        for source_item, translation in zip(batch, translations, strict=True):
            result[source_item["id"]] = translation

    # This should now be an internal invariant, not a model-output dependency.
    missing = [x["id"] for x in items if x["id"] not in result]
    if missing:
        raise RuntimeError(
            "Internal translation mapping error; missing "
            f"{len(missing)} segments: {missing[:10]}"
        )

    return result
