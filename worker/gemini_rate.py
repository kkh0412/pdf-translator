from __future__ import annotations

import os
import re
import queue
import threading
import time
from collections.abc import Callable


_lock = threading.Lock()
_last_started: dict[str, float] = {}
_cooldown_until: dict[str, float] = {}
_daily_exhausted_models: set[str] = set()
_cancel_check: Callable[[], None] | None = None


def set_cancel_check(callback: Callable[[], None] | None) -> None:
    global _cancel_check
    with _lock:
        _cancel_check = callback


def check_cancel() -> None:
    with _lock:
        callback = _cancel_check
    if callback is not None:
        callback()


def run_cancellable_io(callback: Callable[[], object], poll_seconds: float = 0.5):
    """Run blocking network I/O in a daemon thread while honoring cancellation.

    If the browser heartbeat expires, the main worker can unwind immediately
    instead of waiting for a long Gemini socket timeout. The daemon I/O thread
    is terminated when the worker process exits.
    """
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            result_queue.put((True, callback()))
        except BaseException as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()

    while True:
        check_cancel()
        try:
            ok, value = result_queue.get(timeout=max(0.1, poll_seconds))
        except queue.Empty:
            continue
        if ok:
            return value
        raise value  # type: ignore[misc]


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
    """Space request starts while remaining responsive to worker cancellation."""
    interval = 60.0 / _safe_rpm()

    while True:
        check_cancel()
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

        time.sleep(min(remaining, 0.5))
        check_cancel()
