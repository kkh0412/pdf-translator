from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import re
from typing import Callable

from .gemini_rate import check_cancel, run_cancellable_io


PLACEHOLDER_RE = re.compile(r"\[\[MATH_\d+\]\]")
PROTECTED_TOKEN_RE = re.compile(
    r"ZXQPDFTRTOKEN\s*(?P<token>\d+)\s*QXZ",
    flags=re.I,
)


class GoogleTranslateError(RuntimeError):
    pass


class GoogleTranslateTransientError(GoogleTranslateError):
    """The web translation service failed in a way worth retrying later."""


class GoogleTranslateIntegrityError(GoogleTranslateError):
    """A response arrived, but protected content could not be restored safely."""


def configured() -> bool:
    """No API key is required; only the Python package must be installed."""
    if os.getenv("PY_GOOGLE_TRANSLATE_DISABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return False
    return importlib.util.find_spec("googletrans") is not None


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


def _protect_text(text: str, strategy: dict) -> tuple[str, dict[str, str]]:
    """Protect math placeholders and KEEP-ENGLISH terms with opaque text tokens."""
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
        out.append(text[cursor:match.start()])
        key = str(token_index)
        mapping[key] = match.group(0)
        # An intentionally ugly all-caps nonce is much less likely to be
        # translated than a natural-language marker. Restoration is validated.
        out.append(f"ZXQPDFTRTOKEN{key}QXZ")
        token_index += 1
        cursor = match.end()

    out.append(text[cursor:])
    return "".join(out), mapping


def _restore_text(translated: str, mapping: dict[str, str]) -> str:
    translated = str(translated or "")
    seen: set[str] = set()

    def repl(match: re.Match) -> str:
        key = match.group("token")
        if key not in mapping:
            raise GoogleTranslateIntegrityError(
                f"Python Google Translate returned an unknown protected token: {key}"
            )
        seen.add(key)
        return mapping[key]

    restored = PROTECTED_TOKEN_RE.sub(repl, translated)
    missing = sorted(set(mapping) - seen)
    if missing:
        raise GoogleTranslateIntegrityError(
            "Python Google Translate dropped protected tokens: "
            + ", ".join(missing)
        )

    if re.search(r"ZXQPDFTRTOKEN", restored, flags=re.I):
        raise GoogleTranslateIntegrityError(
            "Python Google Translate left an unrecovered protection token"
        )

    return restored


def _target_code(language: str) -> str:
    value = str(language or "").strip()
    mapping = {
        "zh-CN": "zh-cn",
        "zh-TW": "zh-tw",
    }
    return mapping.get(value, value.lower())


def _service_urls() -> list[str]:
    configured_urls = [
        value.strip()
        for value in os.getenv(
            "PY_GOOGLE_TRANSLATE_SERVICE_URLS",
            "translate.googleapis.com,translate.google.com,translate.google.co.kr",
        ).split(",")
        if value.strip()
    ]
    return configured_urls or ["translate.google.com"]


async def _translate_async(
    texts: list[str],
    target_language: str,
) -> list[str]:
    try:
        from googletrans import Translator
    except Exception as exc:
        raise GoogleTranslateError(
            "googletrans is not installed in the worker runtime"
        ) from exc

    timeout = max(
        3.0,
        float(os.getenv("PY_GOOGLE_TRANSLATE_TIMEOUT_SECONDS", "12")),
    )

    try:
        translator = Translator(
            service_urls=_service_urls(),
            raise_exception=True,
            timeout=timeout,
        )

        async def perform(active_translator):
            result = active_translator.translate(
                texts,
                dest=_target_code(target_language),
                src="auto",
            )
            if inspect.isawaitable(result):
                result = await result
            return result

        # googletrans 4.x is async-context-manager based. This fallback keeps
        # compatibility with older/sync implementations as well.
        if hasattr(translator, "__aenter__"):
            async with translator as active:
                result = await perform(active)
        else:
            result = await perform(translator)

    except Exception as exc:
        raise GoogleTranslateTransientError(
            f"TRANSIENT_PY_GOOGLE_TRANSLATE_ERROR: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(result, (list, tuple)):
        result = [result]

    if len(result) != len(texts):
        raise GoogleTranslateTransientError(
            "Python Google Translate returned the wrong number of translations"
        )

    values: list[str] = []
    for item in result:
        value = getattr(item, "text", None)
        if not isinstance(value, str):
            raise GoogleTranslateTransientError(
                "Python Google Translate returned a non-string translation"
            )
        values.append(value)
    return values


def _call_googletrans(
    protected: list[str],
    target_language: str,
) -> list[str]:
    def run() -> list[str]:
        return asyncio.run(
            _translate_async(protected, target_language)
        )

    check_cancel()
    result = run_cancellable_io(run)
    check_cancel()
    return result


def translate_batch(
    batch: list[dict],
    target_language: str,
    strategy: dict,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> list[str]:
    if not configured():
        raise GoogleTranslateError(
            "Python Google Translate fallback is unavailable because "
            "googletrans is not installed"
        )

    protected: list[str] = []
    mappings: list[dict[str, str]] = []
    for item in batch:
        protected_text, mapping = _protect_text(
            item.get("text", ""),
            strategy,
        )
        protected.append(protected_text)
        mappings.append(mapping)

    print(
        "Python Google Translate fallback: "
        f"blocks={len(batch)}, "
        f"chars={sum(len(item.get('text', '')) for item in batch)}",
        flush=True,
    )

    # No deliberate Retry-After / long sleep here. The whole point of this
    # fallback is low latency after a Gemini 429. If the web translator itself
    # is unavailable, preserve the checkpoint and retry on a later worker run.
    raw = _call_googletrans(protected, target_language)
    return [
        _restore_text(value, mapping)
        for value, mapping in zip(raw, mappings)
    ]
