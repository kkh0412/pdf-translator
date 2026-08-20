from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from .gemini_rate import check_cancel, retry_delay_from_text, run_cancellable_io


PLACEHOLDER_RE = re.compile(r"\[\[MATH_\d+\]\]")
SPAN_RE = re.compile(
    r'<span\b(?=[^>]*\bdata-pdftr-token=["\'](?P<token>\d+)["\'])[^>]*>.*?</span>',
    flags=re.I | re.S,
)


class GoogleTranslateError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip())


def _protected_terms(strategy: dict) -> list[str]:
    terms: list[str] = []
    for item in strategy.get("terminology", []) or []:
        if str(item.get("policy", "")).strip() != "keep_english":
            continue
        term = str(item.get("source", "")).strip()
        if term and term not in terms:
            terms.append(term)
    terms.sort(key=len, reverse=True)
    return terms


def _protect_html(text: str, strategy: dict) -> tuple[str, dict[str, str]]:
    """Protect math placeholders and KEEP-ENGLISH terms as HTML elements.

    Cloud Translation does not translate HTML tags. The original token lives in
    our local mapping, not in translatable text, so even if surrounding word
    order changes the math/term itself remains exact.
    """
    text = str(text or "")
    keep_terms = _protected_terms(strategy)

    patterns = [PLACEHOLDER_RE.pattern]
    patterns.extend(re.escape(term) for term in keep_terms if term)
    combined = re.compile("(" + "|".join(patterns) + ")")

    mapping: dict[str, str] = {}
    out: list[str] = []
    cursor = 0
    token_index = 0

    for match in combined.finditer(text):
        out.append(html.escape(text[cursor:match.start()], quote=False))
        original = match.group(0)
        key = str(token_index)
        mapping[key] = original
        # Empty/marker span is inline, immovable content. Both translate=no and
        # class=notranslate are included; the data attribute is our restoration key.
        out.append(
            f'<span translate="no" class="notranslate" '
            f'data-pdftr-token="{key}">PDFTRTOKEN{key}</span>'
        )
        token_index += 1
        cursor = match.end()

    out.append(html.escape(text[cursor:], quote=False))
    return "".join(out), mapping


def _restore_html(translated_html: str, mapping: dict[str, str]) -> str:
    seen: set[str] = set()

    def repl(match: re.Match) -> str:
        key = match.group("token")
        if key not in mapping:
            raise GoogleTranslateError(
                f"Google Translate returned an unknown protected token: {key}"
            )
        seen.add(key)
        return mapping[key]

    restored = SPAN_RE.sub(repl, str(translated_html or ""))
    missing = sorted(set(mapping) - seen)
    if missing:
        raise GoogleTranslateError(
            "Google Translate dropped protected inline tokens: " + ", ".join(missing)
        )

    # Source prose was HTML-escaped before submission; decode entities back to text.
    return html.unescape(restored)


def _sleep_cancellable(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        check_cancel()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


def _call_once(
    api_key: str,
    html_inputs: list[str],
    target_language: str,
) -> list[str]:
    endpoint = "https://translation.googleapis.com/language/translate/v2"
    body = {
        "q": html_inputs,
        "target": target_language,
        "format": "html",
        "model": "nmt",
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Goog-Api-Key": api_key,
        },
    )

    check_cancel()
    try:
        def perform_request():
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))

        payload = run_cancellable_io(perform_request)
        check_cancel()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GoogleTranslateError(
            f"Cloud Translation API HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise GoogleTranslateError(
            f"TRANSIENT_GOOGLE_TRANSLATE_ERROR: {exc}"
        ) from exc

    translations = payload.get("data", {}).get("translations")
    if not isinstance(translations, list) or len(translations) != len(html_inputs):
        raise GoogleTranslateError(
            "Cloud Translation API returned the wrong number of translations"
        )

    result: list[str] = []
    for item in translations:
        value = item.get("translatedText") if isinstance(item, dict) else None
        if not isinstance(value, str):
            raise GoogleTranslateError(
                "Cloud Translation API returned a non-string translation"
            )
        result.append(value)
    return result


def translate_batch(
    batch: list[dict],
    target_language: str,
    strategy: dict,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
    if not api_key:
        raise GoogleTranslateError(
            "GOOGLE_TRANSLATE_API_KEY is not configured"
        )

    protected: list[str] = []
    mappings: list[dict[str, str]] = []
    for item in batch:
        html_text, mapping = _protect_html(item.get("text", ""), strategy)
        protected.append(html_text)
        mappings.append(mapping)

    if status_callback:
        status_callback(
            "번역 요청이 지연되어 Google 번역으로 전환해 계속 처리하고 있습니다."
        )

    print(
        f"Google Translate fallback: blocks={len(batch)}, "
        f"chars={sum(len(item.get('text', '')) for item in batch)}",
        flush=True,
    )

    max_attempts = max(1, int(os.getenv("GOOGLE_TRANSLATE_MAX_RETRIES", "3")))
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        check_cancel()
        try:
            raw = _call_once(api_key, protected, target_language)
            return [
                _restore_html(value, mapping)
                for value, mapping in zip(raw, mappings)
            ]
        except GoogleTranslateError as exc:
            last_error = exc
            text = str(exc)
            # Retry transient throttling / server failures, but not configuration
            # or authorization failures.
            retryable = any(
                marker in text.lower()
                for marker in (
                    "http 429",
                    "http 500",
                    "http 502",
                    "http 503",
                    "http 504",
                    "transient_google_translate_error",
                )
            )
            if not retryable or attempt >= max_attempts:
                raise
            wait = retry_delay_from_text(text, default=min(30.0, 2.0 ** attempt))
            if status_callback:
                status_callback(
                    "Google 번역 요청이 잠시 지연되어 자동으로 다시 시도하고 있습니다."
                )
            _sleep_cancellable(wait)

    raise GoogleTranslateError(str(last_error))
