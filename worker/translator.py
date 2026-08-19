import json
import os
from typing import Iterable

from pydantic import BaseModel


class TranslationItem(BaseModel):
    id: str
    translation: str


class TranslationBatch(BaseModel):
    items: list[TranslationItem]


LANGUAGE_NAMES = {
    "ko": "Korean",
    "en": "English",
    "ja": "Japanese",
    "zh-CN": "Simplified Chinese",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
}


def _chunks(items: list[dict], max_items: int = 80, max_chars: int = 16000) -> Iterable[list[dict]]:
    """Use larger batches to reduce sequential API round trips while staying moderate in size."""
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


def translate_segments(items: list[dict], target_language: str) -> dict[str, str]:
    """Translate text segments while keeping segment IDs stable."""
    if not items:
        return {}

    if os.getenv("MOCK_TRANSLATION", "false").lower() == "true":
        return {item["id"]: f"[{LANGUAGE_NAMES.get(target_language, target_language)}] {item['text']}" for item in items}

    if target_language not in LANGUAGE_NAMES:
        raise ValueError("Unsupported target language")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key == "put_your_api_key_here":
        raise RuntimeError("OPENAI_API_KEY is not configured")

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    result: dict[str, str] = {}

    for batch in _chunks(items):
        compact = [{"id": x["id"], "text": x["text"]} for x in batch]
        prompt = (
            f"Translate every item into {LANGUAGE_NAMES[target_language]}. "
            "Return one translation for every input id. Translate only natural-language content. "
            "Preserve citation markers, reference numbers, URLs, DOIs, acronyms, proper names, and numeric values. "
            "Do not add explanations or Markdown. Prefer compact wording because each translation must fit in the original PDF text box. "
            "Do not omit meaning merely to shorten the translation.\n\n"
            + json.dumps(compact, ensure_ascii=False)
        )
        response = client.responses.parse(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": "You are a precise document translator. Keep translations faithful, compact, and publication-appropriate.",
                },
                {"role": "user", "content": prompt},
            ],
            text_format=TranslationBatch,
            store=False,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("Translation model returned no structured result")
        for item in parsed.items:
            result[item.id] = item.translation.strip()

    missing = [x["id"] for x in items if x["id"] not in result]
    if missing:
        raise RuntimeError(f"Translation response missed {len(missing)} segments")
    return result
