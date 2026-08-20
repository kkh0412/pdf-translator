from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
import unicodedata
import urllib.error
import urllib.request
from typing import Callable, Iterable

from .gemini_rate import (
    check_cancel,
    daily_quota_exhausted,
    is_daily_quota_error,
    mark_daily_quota_exhausted,
    run_cancellable_io,
    wait_for_slot,
)
from .google_translate import (
    GoogleTranslateError,
    GoogleTranslateIntegrityError,
    GoogleTranslateTransientError,
    configured as google_translate_configured,
    translate_batch as google_translate_batch,
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
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"Gemini API HTTP {status}: {detail}")


class GeminiTransportError(RuntimeError):
    """Transient network/protocol failure while calling Gemini."""


class GeminiOutputError(RuntimeError):
    pass


class TranslationValidationError(RuntimeError):
    """Provider-neutral validation failure for a translated prose block."""


class GeminiDailyQuotaError(RuntimeError):
    def __init__(self, model: str, detail: str):
        self.model = model
        self.detail = detail
        super().__init__(f"Daily quota exhausted for {model}: {detail}")


class Gemini429UseGoogle(RuntimeError):
    """Translation-only signal: do not wait; switch this job to Google Translate."""

    def __init__(self, model: str, detail: str):
        self.model = model
        self.detail = detail
        super().__init__(f"Gemini translation 429 on {model}")


class TranslationRoute:
    """Per-job translation provider state.

    A module-global switch is unsafe for checkpoint/resume because a new worker
    process would forget that this particular job already hit Gemini 429.  Keep
    the route state explicit and persist the Google lock in the job checkpoint.
    """

    def __init__(self, *, force_google: bool = False):
        self._google_locked = threading.Event()
        self._google_persistent = threading.Event()
        if force_google:
            self._google_locked.set()
            self._google_persistent.set()

    @property
    def google_locked(self) -> bool:
        return self._google_locked.is_set()

    @property
    def persistent_google_lock(self) -> bool:
        """Whether this route lock must survive a worker restart."""
        return self._google_persistent.is_set()

    def lock_google(self, *, persistent: bool = False) -> bool:
        """Lock the rest of this translation stage to Google.

        Returns True only for the first transition.  The caller can use that
        edge to persist provider state without repeatedly writing checkpoints.
        """
        if persistent:
            self._google_persistent.set()
        if self._google_locked.is_set():
            return False
        self._google_locked.set()
        return True


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
        raise TranslationValidationError(
            "Translation leaked an internal math/control marker"
        )

    if target_language == "ko" and re.search(
        r"(?:[가-힣]\s+){5,}[가-힣]",
        translated,
    ):
        raise TranslationValidationError(
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
            raise TranslationValidationError(
                "Translation appears to be an untranslated English prose block"
            )


def _validate_math_placeholders(source: str, translated: str) -> None:
    """Require exact preservation of every inline-math placeholder.

    Punctuation adjacency is intentionally *not* validated.  Word order and
    sentence-final placement legitimately change across languages (especially
    English -> Korean), while the actual mathematical integrity requirement is
    that placeholders are neither edited, dropped, duplicated, nor invented.
    """
    expected = sorted(PLACEHOLDER_RE.findall(source))
    actual = sorted(PLACEHOLDER_RE.findall(translated))
    if expected != actual:
        raise TranslationValidationError(
            f"Math placeholders changed: expected={expected}, actual={actual}"
        )


def _validate_translation_result(
    source: str,
    translated: str,
    target_language: str,
) -> None:
    _validate_math_placeholders(source, translated)
    _validate_translation_quality(source, translated, target_language)


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
    check_cancel()
    wait_for_slot(model)
    check_cancel()

    try:
        def perform_request():
            with urllib.request.urlopen(req, timeout=150) as response:
                return json.loads(response.read().decode("utf-8"))

        payload = run_cancellable_io(perform_request)
        check_cancel()
        return payload
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GeminiHTTPError(exc.code, detail) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GeminiTransportError(f"TRANSIENT_GEMINI_ERROR: {exc}") from exc


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
            "previous_context": item.get("previous_context", ""),
            "next_context": item.get("next_context", ""),
            "continues_from_previous": bool(item.get("continues_from_previous", False)),
            "continues_to_next": bool(item.get("continues_to_next", False)),
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
        "natural grammar, but never edit, merge, duplicate, translate, or delete it. "
        "An inline math placeholder is a grammatical constituent INSIDE the surrounding sentence, "
        "not a sentence separator. Translate the complete sentence around it coherently. Never add "
        "a period or other sentence-ending punctuation merely because inline mathematics appears "
        "between prose fragments.\n"
        "previous_context and next_context are READ-ONLY context. Never copy them into the returned "
        "translation. If continues_from_previous or continues_to_next is true, preserve the grammatical "
        "continuation across the neighboring block/display equation instead of forcing the current "
        "fragment into a standalone sentence.\n"
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

        try:
            _validate_translation_result(
                item["text"],
                value,
                target_language,
            )
        except TranslationValidationError as exc:
            raise GeminiOutputError(str(exc)) from exc

        out.append(value)

    return out



def _translate_with_quota_retries(
    api_key: str,
    model: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
    route: TranslationRoute,
    *,
    context: str,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    """Translation policy: a Gemini 429 never waits.

    Vision/math repair still has its own retry policy because Google Translate
    cannot replace those tasks. This function is used only for natural-language
    translation, where Google Translate is an acceptable immediate fallback.
    """
    # A previous batch in this job may have hit 429 while this batch was waiting
    # for recovery.  Never start a fresh Gemini request after the route is locked.
    if route.google_locked:
        if google_translate_configured():
            raise Gemini429UseGoogle(
                model,
                "This translation job is already locked to Google Translate.",
            )
        raise RuntimeError(
            "TRANSIENT_GOOGLE_TRANSLATE_ERROR: this translation job is locked "
            "to Google Translate, but the googletrans runtime is unavailable."
        )

    if daily_quota_exhausted(model):
        raise GeminiDailyQuotaError(
            model,
            "This model already reached its per-day quota earlier in this job.",
        )

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

        # Do NOT impose a cooldown or honor Retry-After for prose translation.
        # User latency is more important here because Google Translate can take
        # over this exact task immediately.
        # A live prose-translation 429 is a one-way route change for this job.
        # Never probe another Gemini model after this point, even if the Google
        # runtime is unexpectedly missing; the runtime verifier should normally
        # make that state impossible.
        route.lock_google(persistent=True)
        if google_translate_configured():
            if status_callback:
                status_callback(
                    "번역 요청이 지연되어 Google 번역으로 전환해 계속 처리하고 있습니다."
                )
            print(
                f"{context}: Gemini 429 on {model}; switching immediately "
                "to Google Translate without waiting.",
                flush=True,
            )
            raise Gemini429UseGoogle(model, exc.detail) from exc

        raise RuntimeError(
            "TRANSIENT_GOOGLE_TRANSLATE_ERROR: Gemini translation returned 429, "
            "but the required Python Google Translate runtime is unavailable."
        ) from exc


def _google_translate_fallback(
    batch: list[dict],
    target_language: str,
    strategy: dict,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    if not google_translate_configured():
        raise RuntimeError(
            "TRANSIENT_GOOGLE_TRANSLATE_ERROR: Python Google Translate is required "
            "for this translation route, but the googletrans runtime is unavailable."
        )

    values = google_translate_batch(
        batch,
        target_language,
        strategy,
        status_callback=status_callback,
    )
    if len(values) != len(batch):
        raise GoogleTranslateError(
            "Google Translate fallback returned the wrong number of blocks"
        )

    cleaned: list[str] = []
    for item, value in zip(batch, values):
        value = _sanitize(value)
        if not value:
            raise GoogleTranslateIntegrityError(
                "Google Translate fallback returned an empty translation"
            )

        try:
            _validate_translation_result(
                item["text"],
                value,
                target_language,
            )
        except TranslationValidationError as exc:
            raise GoogleTranslateIntegrityError(str(exc)) from exc
        cleaned.append(value)

    return cleaned

def _request_batch(
    api_key: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
    route: TranslationRoute,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    """Translate a batch with zero-wait Google fallback on any Gemini 429."""
    if route.google_locked:
        return _google_translate_fallback(
            batch,
            target_language,
            strategy,
            status_callback=status_callback,
        )

    for model in _candidate_models():
        if route.google_locked:
            return _google_translate_fallback(
                batch,
                target_language,
                strategy,
                status_callback=status_callback,
            )

        if daily_quota_exhausted(model):
            continue

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
                route,
                context="Translation",
                status_callback=status_callback,
            )

        except Gemini429UseGoogle:
            return _google_translate_fallback(
                batch,
                target_language,
                strategy,
                status_callback=status_callback,
            )

        except GeminiDailyQuotaError:
            # Daily exhaustion known before the call can still move to another
            # model. A live 429 with Google configured never reaches here.
            continue

        except GeminiHTTPError as exc:
            if exc.status in {404, 500, 502, 503, 504}:
                if status_callback:
                    status_callback(
                        "번역을 계속하기 위해 다른 처리 경로로 전환하고 있습니다."
                    )
                continue
            raise

        except GeminiOutputError:
            # Structured/content quality problems are handled by _recover,
            # which can isolate the offending semantic block.
            raise

        except GeminiTransportError:
            # Network/protocol failures may use another configured model, but
            # unrelated RuntimeError bugs must not be silently swallowed here.
            if status_callback:
                status_callback(
                    "번역을 계속하기 위해 다른 처리 경로로 전환하고 있습니다."
                )
            continue

    # All Gemini translation paths are exhausted/unavailable. As the final
    # prose-translation fallback, use the local Python googletrans fallback.
    # Vision/math reconstruction still remains Gemini/source-PDF based.
    if google_translate_configured():
        if route.lock_google() and status_callback:
            status_callback(
                "Gemini 번역 경로를 사용할 수 없어 Google 번역으로 계속 처리하고 있습니다."
            )
    return _google_translate_fallback(
        batch,
        target_language,
        strategy,
        status_callback=status_callback,
    )


def _handle_google_error(
    exc: GoogleTranslateError,
    api_key: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
    route: TranslationRoute,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    if isinstance(exc, GoogleTranslateIntegrityError):
        # A malformed response for one item must not discard an otherwise valid
        # multi-block batch. Isolate the offending block without going back to
        # Gemini after the job has entered Google mode.
        if len(batch) > 1:
            mid = len(batch) // 2
            return (
                _google_recoverable(
                    api_key,
                    batch[:mid],
                    target_language,
                    strategy,
                    route,
                    status_callback=status_callback,
                )
                + _google_recoverable(
                    api_key,
                    batch[mid:],
                    target_language,
                    strategy,
                    route,
                    status_callback=status_callback,
                )
            )
        raise RuntimeError(
            "TRANSIENT_GOOGLE_TRANSLATE_ERROR: Google Translate returned a "
            f"single-block result that failed integrity validation: {exc}"
        ) from exc

    if isinstance(exc, GoogleTranslateTransientError):
        raise RuntimeError(
            f"TRANSIENT_GOOGLE_TRANSLATE_ERROR: {exc}"
        ) from exc

    raise RuntimeError(
        f"TRANSIENT_GOOGLE_TRANSLATE_ERROR: {exc}"
    ) from exc


def _google_recoverable(
    api_key: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
    route: TranslationRoute,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    """Run Google translation with structural isolation and transient tagging."""
    if route.lock_google() and status_callback:
        status_callback(
            "Google 번역으로 본문을 계속 처리하고 있습니다."
        )
    try:
        return _google_translate_fallback(
            batch,
            target_language,
            strategy,
            status_callback=status_callback,
        )
    except GoogleTranslateError as exc:
        return _handle_google_error(
            exc,
            api_key,
            batch,
            target_language,
            strategy,
            route,
            status_callback=status_callback,
        )

def _recover(
    api_key: str,
    batch: list[dict],
    target_language: str,
    strategy: dict,
    route: TranslationRoute,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    try:
        return _request_batch(
            api_key,
            batch,
            target_language,
            strategy,
            route,
            status_callback=status_callback,
        )

    except GoogleTranslateError as exc:
        # _request_batch can enter here directly when the route was already
        # Google-locked. Classify the response without re-sending the same whole
        # batch; integrity failures are split immediately into smaller batches.
        return _handle_google_error(
            exc,
            api_key,
            batch,
            target_language,
            strategy,
            route,
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
                    route,
                    status_callback=status_callback,
                )
                + _recover(
                    api_key,
                    batch[mid:],
                    target_language,
                    strategy,
                    route,
                    status_callback=status_callback,
                )
            )

        # If any concurrent batch has already observed a live 429, quality
        # recovery is Google-only. This closes the old path that could call a
        # second Gemini model after the job had switched providers.
        if route.google_locked:
            return _google_recoverable(
                api_key,
                batch,
                target_language,
                strategy,
                route,
                status_callback=status_callback,
            )

        # One block failed Gemini content/structured-output validation. Try
        # alternate models only while the job is still in Gemini mode.
        models = _candidate_models()
        for model in models[1:]:
            if route.google_locked:
                return _google_recoverable(
                    api_key,
                    batch,
                    target_language,
                    strategy,
                    route,
                    status_callback=status_callback,
                )
            if daily_quota_exhausted(model):
                continue

            try:
                if status_callback:
                    status_callback(
                        "문장 품질을 확인하며 다른 번역 모델로 다시 처리하고 있습니다."
                    )
                return _translate_with_quota_retries(
                    api_key,
                    model,
                    batch,
                    target_language,
                    strategy,
                    route,
                    context="Single-block quality fallback",
                    status_callback=status_callback,
                )
            except Gemini429UseGoogle:
                return _google_recoverable(
                    api_key,
                    batch,
                    target_language,
                    strategy,
                    route,
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
            except GeminiTransportError:
                continue

        # Last prose fallback after Gemini quality failure. Lock the route so
        # recursive recovery cannot cycle back into the Gemini model chain.
        return _google_recoverable(
            api_key,
            batch,
            target_language,
            strategy,
            route,
            status_callback=status_callback,
        )

def translate_blocks(
    items: list[dict],
    target_language: str,
    strategy: dict,
    progress_callback: Callable[[float, str], None] | None = None,
    initial_translations: dict[str, str] | None = None,
    checkpoint_callback: Callable[[dict[str, str]], None] | None = None,
    *,
    force_google_fallback: bool = False,
    route_state_callback: Callable[[bool], None] | None = None,
) -> dict[str, str]:
    # Provider routing is scoped to this translation job and can be restored
    # from its persistent checkpoint.  This prevents a resumed job from probing
    # Gemini again after an earlier live 429.
    route = TranslationRoute(force_google=force_google_fallback)
    route_notified = force_google_fallback

    def notify_route_state() -> None:
        nonlocal route_notified
        if route.persistent_google_lock and not route_notified:
            route_notified = True
            if route_state_callback:
                route_state_callback(True)

    if not items:
        return {}

    initial = dict(initial_translations or {})
    item_by_id = {item["id"]: item for item in items}
    result: dict[str, str] = {}

    for key, value in initial.items():
        check_cancel()
        item = item_by_id.get(key)
        if item is None or not isinstance(value, str) or not value.strip():
            continue
        try:
            _validate_translation_result(item["text"], value, target_language)
        except TranslationValidationError:
            print(
                f"Checkpoint translation {key} failed current integrity/quality rules; "
                "retranslating only this block.",
                flush=True,
            )
            continue
        result[key] = value

    if os.getenv("MOCK_TRANSLATION", "false").lower() == "true":
        result.update({item["id"]: item["text"] for item in items})
        if checkpoint_callback:
            checkpoint_callback(dict(result))
        return result

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key and not route.google_locked:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    if target_language not in LANGUAGE_NAMES:
        raise RuntimeError(f"Unsupported target language: {target_language}")

    if route.google_locked:
        print(
            "Translation resume policy: Google Translate is already locked for "
            "this job from an earlier Gemini 429/fallback checkpoint.",
            flush=True,
        )

    pending_items = [item for item in items if item["id"] not in result]
    total_blocks = max(1, len(items))
    completed_blocks = len(items) - len(pending_items)

    if progress_callback and completed_blocks:
        progress_callback(
            completed_blocks / total_blocks,
            f"저장된 번역 · {completed_blocks}/{len(items)}개 블록 완료 · "
            "남은 부분부터 이어서 진행합니다.",
        )

    if not pending_items:
        return result

    batches = list(_chunks(pending_items))
    workers = max(1, min(2, int(os.getenv("TRANSLATION_WORKERS", "2"))))
    print(
        f"Translation resume plan: {completed_blocks}/{len(items)} blocks saved, "
        f"remaining={len(pending_items)}, batches={len(batches)}, workers={workers}, "
        f"google_locked={route.google_locked}",
        flush=True,
    )

    lock = threading.Lock()
    completed_batches = 0

    def emit(fraction: float, message: str) -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)), message)

    def run_one(index: int, batch: list[dict]) -> list[str]:
        check_cancel()
        with lock:
            visible_fraction = min(
                0.96,
                (completed_blocks + min(len(batch) * 0.18, 2.0)) / total_blocks,
            )
            emit(
                visible_fraction,
                f"본문 번역 묶음 {index + 1}/{len(batches)} 처리 중 · "
                f"{len(batch)}개 블록",
            )

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
            route,
            status_callback=model_status,
        )

    def mark_complete(
        batch: list[dict],
        translated: list[str],
    ) -> None:
        nonlocal completed_blocks, completed_batches

        # This coordinator is the only place that mutates/persists result.
        # In the threaded path future.result() and this function both run on the
        # coordinator thread, so checkpoint snapshots are deterministic.
        with lock:
            for item, value in zip(batch, translated):
                result[item["id"]] = value
            completed_blocks += len(batch)
            completed_batches += 1
            snapshot = dict(result)
            emit(
                completed_blocks / total_blocks,
                f"본문 번역 · {completed_blocks}/{len(items)}개 블록 완료 · "
                f"완료된 묶음 {completed_batches}/{len(batches)}",
            )

        notify_route_state()
        if checkpoint_callback:
            checkpoint_callback(snapshot)

    try:
        if workers == 1 or len(batches) <= 1:
            for index, batch in enumerate(batches):
                translated = run_one(index, batch)
                mark_complete(batch, translated)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_index = {
                    executor.submit(run_one, index, batch): index
                    for index, batch in enumerate(batches)
                }
                for future in concurrent.futures.as_completed(future_to_index):
                    index = future_to_index[future]
                    translated = future.result()
                    mark_complete(batches[index], translated)
    except Exception:
        # Persist the provider lock and every successfully completed block before
        # propagating a transient/cancellation error to run_job.
        notify_route_state()
        if checkpoint_callback:
            checkpoint_callback(dict(result))
        raise

    notify_route_state()

    missing = [item["id"] for item in items if item["id"] not in result]
    if missing:
        raise RuntimeError(
            "Translation checkpoint merge missed blocks: "
            + ", ".join(missing[:12])
        )

    return result
