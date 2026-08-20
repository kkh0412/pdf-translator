from __future__ import annotations

import os
import re
import threading
import time


_lock = threading.Lock()
_last_started: dict[str, float] = {}
_cooldown_until: dict[str, float] = {}
_daily_exhausted_models: set[str] = set()


def _safe_rpm() -> float:
    raw = os.getenv("GEMINI_SAFE_RPM", "18").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 18.0
    return max(1.0, value)


def retry_delay_from_text(detail: str, default: float = 60.0) -> float:
    text = str(detail or "")
    for pattern in (
        r"retry\s+in\s+([0-9]+(?:\.[0-9]+)?)s",
        r'"retryDelay"\s*:\s*"([0-9]+(?:\.[0-9]+)?)s"',
        r'"retry_after"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
    ):
        match = re.search(pattern, text, flags=re.I)
        if match:
            return max(1.0, float(match.group(1)))
    return max(1.0, float(default))


def is_daily_quota_error(detail: str) -> bool:
    """Detect a per-day / RPD quota from Gemini's 429 error payload.

    Daily model quotas must not be treated like a minute-level throttle:
    sleeping for Retry-After and calling the same model again only wastes time.
    """
    text = str(detail or "").lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)

    daily_markers = (
        "generaterequestsperday",
        "requestsperday",
        "perdayperprojectpermodel",
        "perdayperproject",
        "requestsperdayperproject",
    )
    return any(marker in compact for marker in daily_markers)


def mark_daily_quota_exhausted(model: str) -> None:
    model = str(model or "").strip()
    if not model:
        return
    with _lock:
        _daily_exhausted_models.add(model)


def daily_quota_exhausted(model: str) -> bool:
    model = str(model or "").strip()
    with _lock:
        return model in _daily_exhausted_models


def daily_exhausted_models() -> tuple[str, ...]:
    with _lock:
        return tuple(sorted(_daily_exhausted_models))


def impose_cooldown(model: str, seconds: float) -> None:
    seconds = max(1.0, float(seconds))
    with _lock:
        _cooldown_until[model] = max(
            _cooldown_until.get(model, 0.0),
            time.monotonic() + seconds,
        )


def wait_for_slot(model: str) -> None:
    """Space request starts across local Vision/translation worker threads."""
    interval = 60.0 / _safe_rpm()

    while True:
        with _lock:
            now = time.monotonic()
            earliest = max(
                _last_started.get(model, 0.0) + interval,
                _cooldown_until.get(model, 0.0),
            )
            remaining = earliest - now
            if remaining <= 0:
                _last_started[model] = now
                return

        time.sleep(min(remaining, 1.0))
