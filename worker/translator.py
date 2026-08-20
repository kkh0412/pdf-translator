from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
import threading
import unicodedata
import urllib.error
import urllib.request
from typing import Callable, Iterable

from .gemini_rate import (
    daily_quota_exhausted,
    impose_cooldown,
    is_daily_quota_error,
    mark_daily_quota_exhausted,
    retry_delay_from_text,
    wait_for_slot,
)


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
    def __init__(self, status: int, detail: str, retry_after: float | None = None):
        self.status = status
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(f"Gemini API HTTP {status}: {detail}")


class GeminiOutputError(RuntimeError):
    pass


class GeminiDailyQuotaError(RuntimeError):
    def __init__(self, model: str, detail: str):
        self.model = model
        self.detail = detail
        super().__init__(f"Daily quota exhausted for {model}: {detail}")


def _sanitize(value: str) -> str:
    value = unicodedata.normalize("NFC", value or "")
    out = []

    for ch in value:
        if unicodedata.category(ch).startswith("C"):
            if ch in {"\n", "\t", "\r"}:
                out.append(" ")
            continue
        out.append(ch)

    value = "".join(out)
    value = value.replace("\u00a0", " ").replace("\u00ad", "")
    value = re.sub(r"[ \t\r\n]+", " ", value)
    return value.strip()


def _latin_count(text: str) -> int:
    return sum(
        1
        for ch in text
        if ("A" <= ch <= "Z") or ("a" <= ch <= "z")
    )


def _hangul_count(text: str) -> int:
    return sum(1 for ch in text if "\uac00" <= ch <= "\ud7a3")


def _validate_translation_quality(
    source: str,
    translated: str,
    target_language: str,
) -> None:
    # Reject internal transport COMMANDS, not every literal § character.
    # A section sign can be legitimate prose, and Vision may occasionally confuse
    # a Unicode letter such as Š with §; source-hint repair runs before this stage.
    forbidden = (
        "§math{",
        "§mathcal",
        "§gamma",
        "§rho",
        "§Pi",
        "§Sigma",
        "\\math{",
        "\ufffd",
        "\ufffe",
        "\uffff",
    )
    if any(token in translated for token in forbidden):
        raise GeminiOutputError(
            "Translation leaked an internal math/control marker"
        )

    if target_language == "ko" and re.search(
        r"(?:[가-힣]\s+){5,}[가-힣]",
        translated,
    ):
        raise GeminiOutputError(
            "Translation contains suspicious syllable-by-syllable Korean spacing"
        )

    if target_language == "ko":
        source_latin = _latin_count(source)
        out_latin = _latin_count(translated)
        out_hangul = _hangul_count(translated)

        if (
            source_latin >= 60
            and len(translated) >= 80
            and out_latin >= 45
            and out_hangul < max(8, int(out_latin * 0.18))
        ):
            raise GeminiOutputError(
                "Translation appears to be an untranslated English prose block"
            )


def _candidate_models() -> list[str]:
    primary = os.getenv(
        "GEMINI_TRANSLATION_MODEL",
        "gemini-3.5-flash-lite",
    ).strip()

    configured = [
        item.strip()
        for item in os.getenv("GEMINI_TRANSLATION_MODELS", "").split(",")
        if item.strip()
    ]

    models = [
        primary,
        *configured,
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
    ]

    out: list[str] = []
    for model in models:
        if model and model not in out:
            out.append(model)
    return out


def _chunks(
    items: list[dict],
    max_items: int = 10,
    max_chars: int = 4200,
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
    wait_for_slot(model)

    try:
        with urllib.request.urlopen(req, timeout=150) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        retry_after = None

        if exc.code == 429:
            header_value = exc.headers.get("Retry-After") if exc.headers else None
            if header_value:
                try:
                    retry_after = float(header_value)
                except ValueError:
                    retry_after = None
            if retry_after is None:
                retry_after = retry_delay_from_text(detail, default=60.0)

        raise GeminiHTTPError(
            exc.code,
            detail,
            retry_after=retry_after,
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TRANSIENT_GEMINI_ERROR: {exc}") from exc


def _strategy_prompt(strategy: dict) -> str:
    glossary = strategy.get("terminology", [])
    glossary_lines = []
    for term in glossary:
        source = str(term.get("source", "")).strip()
        target = str(term.get("target", "")).strip()
        note = str(term.get("note", "")).strip()
        policy = str(term.get("policy", "translate")).strip()
        if source and target:
            if policy == "keep_english":
                rendered = f"- {source} => KEEP ENGLISH EXACTLY AS: {source}"
            else:
                rendered = f"- {source} => {target}"
            glossary_lines.append(
                rendered + (f" ({note})" if note else "")
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
        "When a glossary entry applies, obey its policy consistently.\n"
        "For policy=KEEP ENGLISH, preserve that English concept name exactly. Do not transliterate "
        "it, do not force a Korean translation, and do not add a parenthesized Korean gloss unless "
        "the source text already contains one.\n"
        "For Korean technical prose more generally: if a specialized concept is not in the glossary "
        "and there is no clearly established Korean term recognizable beyond the narrow subfield, "
        "prefer the original English concept name rather than inventing an awkward Korean rendering. "
        "Broad textbook-level terms may be translated naturally.\n"
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

        _validate_translation_quality(
            item["text"],
            value,
            target_language,
        )

        out.append(value)

    return out



def _translate_with_quota_retries(
    api_key: str,
    model: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
    *,
    context: str,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    """Use one model while distinguishing daily quota from temporary throttling."""
    if daily_quota_exhausted(model):
        raise GeminiDailyQuotaError(
            model,
            "This model already reached its per-day quota earlier in this job.",
        )

    max_429 = max(
        1,
        int(os.getenv("GEMINI_MAX_429_RETRIES", "8")),
    )
    quota_attempt = 0

    while True:
        try:
            return _translate_once(
                api_key,
                model,
                batch,
                target_language,
                strategy,
            )

        except GeminiHTTPError as exc:
            if exc.status != 429:
                raise

            if is_daily_quota_error(exc.detail):
                mark_daily_quota_exhausted(model)
                if status_callback:
                    status_callback(
                        "현재 처리 경로의 사용량이 많아 다른 경로로 전환하고 있습니다."
                    )
                print(
                    f"Daily quota exhausted for {model}; switching models immediately.",
                    flush=True,
                )
                raise GeminiDailyQuotaError(model, exc.detail) from exc

            quota_attempt += 1
            if quota_attempt > max_429:
                raise RuntimeError(
                    f"{context}: temporary Gemini rate limit did not recover after "
                    f"{max_429} waits on model {model}."
                ) from exc

            wait_seconds = max(
                2.0,
                float(
                    exc.retry_after
                    or retry_delay_from_text(exc.detail)
                ),
            ) + 1.0

            impose_cooldown(model, wait_seconds)
            if status_callback:
                status_callback(
                    "현재 요청이 많아 잠시 기다린 뒤 자동으로 계속합니다."
                )
            print(
                f"{context}: transient 429 on {model}; waiting "
                f"{wait_seconds:.1f}s before retry "
                f"{quota_attempt}/{max_429}.",
                flush=True,
            )

            # _translate_once -> _call -> wait_for_slot() observes the shared
            # cooldown. Daily quota never reaches this branch.


def _request_batch(
    api_key: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    """Try the configured model chain, skipping models whose RPD is exhausted."""
    last_error: Exception | None = None
    attempted_models = 0

    for model in _candidate_models():
        if daily_quota_exhausted(model):
            continue

        attempted_models += 1

        try:
            print(
                f"Translation model attempt: {model}, blocks={len(batch)}",
                flush=True,
            )
            return _translate_with_quota_retries(
                api_key,
                model,
                batch,
                target_language,
                strategy,
                context="Translation",
                status_callback=status_callback,
            )

        except GeminiDailyQuotaError as exc:
            last_error = exc
            # The next model has an independent model quota dimension.
            continue

        except GeminiHTTPError as exc:
            last_error = exc
            if exc.status in {404, 500, 502, 503, 504}:
                if status_callback:
                    status_callback(
                        "번역을 계속하기 위해 다른 처리 경로로 전환하고 있습니다."
                    )
                continue
            raise

        except RuntimeError as exc:
            # Temporary 429 retry budget or transport/network errors.
            last_error = exc
            if status_callback:
                status_callback(
                    "번역을 계속하기 위해 다른 처리 경로로 전환하고 있습니다."
                )
            continue

        except GeminiOutputError:
            # Structured/content quality problems should be handled by _recover
            # through smaller semantic batches rather than silently switching.
            raise

    if attempted_models == 0:
        raise RuntimeError(
            "TRANSIENT_GEMINI_ERROR: all configured translation models "
            "have reached their daily quota for this job."
        )

    raise RuntimeError(
        "TRANSIENT_GEMINI_ERROR: no configured translation model is currently "
        f"available. Last error: {last_error}"
    )


def _recover(
    api_key: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    try:
        return _request_batch(
            api_key,
            batch,
            target_language,
            strategy,
            status_callback=status_callback,
        )

    except GeminiOutputError as exc:
        if len(batch) > 1:
            mid = len(batch) // 2
            return (
                _recover(
                    api_key,
                    batch[:mid],
                    target_language,
                    strategy,
                    status_callback=status_callback,
                )
                + _recover(
                    api_key,
                    batch[mid:],
                    target_language,
                    strategy,
                    status_callback=status_callback,
                )
            )

        # One block failed content/structured-output validation. Try alternate
        # currently available models, still respecting per-model daily quotas.
        models = _candidate_models()
        for model in models[1:]:
            if daily_quota_exhausted(model):
                continue

            try:
                if status_callback:
                    status_callback(
                        "문장 품질을 확인하며 다른 처리 경로로 다시 번역하고 있습니다."
                    )
                return _translate_with_quota_retries(
                    api_key,
                    model,
                    batch,
                    target_language,
                    strategy,
                    context="Single-block quality fallback",
                    status_callback=status_callback,
                )
            except GeminiDailyQuotaError:
                continue
            except GeminiHTTPError as fallback_http:
                if fallback_http.status in {404, 500, 502, 503, 504}:
                    continue
                raise
            except GeminiOutputError:
                continue
            except RuntimeError:
                continue

        raise RuntimeError(
            "Translation failed quality validation for one block after trying "
            "all currently available translation paths. The document is stopped "
            "instead of being emitted partly untranslated."
        ) from exc

    except Exception:
        raise


def translate_blocks(
    items: list[dict],
    target_language: str,
    strategy: dict,
    progress_callback: Callable[[float, str], None] | None = None,
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
    completed_blocks = 0
    started_batches = 0
    lock = threading.Lock()
    total_blocks = max(1, len(items))

    def emit(fraction: float, message: str) -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)), message)

    def run_one(index: int, batch: list[dict]) -> list[str]:
        nonlocal started_batches
        with lock:
            started_batches += 1
            start_number = started_batches
            # Starting a request moves a small amount within the current
            # translation range even before the remote call finishes.
            visible_fraction = min(
                0.96,
                (completed_blocks + min(len(batch) * 0.18, 2.0)) / total_blocks,
            )
            emit(
                visible_fraction,
                f"번역 요청 {start_number}/{len(batches)} 처리 중 · "
                f"{len(batch)}개 텍스트 블록",
            )

        try:
            def model_status(message: str) -> None:
                with lock:
                    current_fraction = max(
                        completed_blocks / total_blocks,
                        min(0.96, visible_fraction),
                    )
                    emit(current_fraction, message)

            return _recover(
                api_key,
                batch,
                target_language,
                strategy,
                status_callback=model_status,
            )
        except Exception:
            with lock:
                emit(
                    completed_blocks / total_blocks,
                    f"번역 요청 {index + 1}/{len(batches)}를 더 작은 묶음으로 복구 중",
                )
            raise

    def mark_complete(batch: list[dict], index: int) -> None:
        nonlocal completed_blocks
        with lock:
            completed_blocks += len(batch)
            emit(
                completed_blocks / total_blocks,
                f"본문 번역 · {completed_blocks}/{len(items)}개 블록 완료 "
                f"({index + 1}/{len(batches)} 묶음)",
            )

    if workers == 1 or len(batches) <= 1:
        for index, batch in enumerate(batches):
            translated_batches[index] = run_one(index, batch)
            mark_complete(batch, index)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_index = {
                executor.submit(run_one, index, batch): index
                for index, batch in enumerate(batches)
            }
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                translated_batches[index] = future.result()
                mark_complete(batches[index], index)

    result: dict[str, str] = {}
    for batch, translated in zip(batches, translated_batches):
        assert translated is not None
        for item, value in zip(batch, translated):
            result[item["id"]] = value

    return result
