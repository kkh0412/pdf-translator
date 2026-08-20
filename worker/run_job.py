from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import threading
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from supabase import create_client

from .pdf_pipeline import process_pdf
from .gemini_rate import set_cancel_check


MAX_STORAGE_BYTES = 50 * 1024 * 1024
CHECKPOINT_BUCKET = "translation-checkpoints"
CHECKPOINT_VERSION = 1
CLIENT_HEARTBEAT_TIMEOUT_SECONDS = max(20, int(os.getenv("CLIENT_HEARTBEAT_TIMEOUT_SECONDS", "45")))
CLIENT_HEARTBEAT_POLL_SECONDS = 5.0


class ClientDisconnectedError(RuntimeError):
    pass


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _heartbeat_is_stale(value: object, *, now_value: datetime | None = None) -> bool:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return True
    current = now_value or datetime.now(timezone.utc)
    return (current - parsed).total_seconds() > CLIENT_HEARTBEAT_TIMEOUT_SECONDS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checkpoint_jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _checkpoint_jsonable(item)
            for key, item in value.items()
            if key != "asset"
        }
    if isinstance(value, (list, tuple)):
        return [_checkpoint_jsonable(item) for item in value]
    return value


def _load_checkpoint(
    db,
    checkpoint_path: str,
    *,
    source_sha256: str,
    target_language: str,
) -> dict | None:
    try:
        compressed = db.storage.from_(CHECKPOINT_BUCKET).download(checkpoint_path)
    except Exception as exc:
        text = str(exc).lower()
        if "not found" not in text and "404" not in text:
            print(f"Checkpoint download skipped: {exc}", flush=True)
        return None

    try:
        payload = json.loads(gzip.decompress(compressed).decode("utf-8"))
    except Exception as exc:
        print(f"Checkpoint is unreadable and will be ignored: {exc}", flush=True)
        return None

    if payload.get("version") != CHECKPOINT_VERSION:
        print("Checkpoint version mismatch; starting this job from structure analysis.", flush=True)
        return None
    if payload.get("source_sha256") != source_sha256:
        print("Checkpoint source fingerprint mismatch; ignoring stale checkpoint.", flush=True)
        return None
    if payload.get("target_language") != target_language:
        print("Checkpoint language mismatch; ignoring stale checkpoint.", flush=True)
        return None

    state = payload.get("state")
    return state if isinstance(state, dict) else None


def _save_checkpoint(
    db,
    checkpoint_path: str,
    state: dict,
    *,
    source_sha256: str,
    target_language: str,
) -> int:
    payload = {
        "version": CHECKPOINT_VERSION,
        "source_sha256": source_sha256,
        "target_language": target_language,
        "saved_at": now(),
        "state": _checkpoint_jsonable(state),
    }
    compressed = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=6,
    )
    if len(compressed) > MAX_STORAGE_BYTES:
        raise RuntimeError(
            "Checkpoint became unexpectedly large and cannot be persisted safely."
        )

    db.storage.from_(CHECKPOINT_BUCKET).upload(
        checkpoint_path,
        compressed,
        {
            "content-type": "application/gzip",
            "upsert": "true",
        },
    )
    return len(compressed)


def _delete_checkpoint(db, checkpoint_path: str) -> None:
    try:
        db.storage.from_(CHECKPOINT_BUCKET).remove([checkpoint_path])
    except Exception as exc:
        print(f"Checkpoint cleanup skipped: {exc}", flush=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def _mb(n: int) -> float:
    return n / (1024 * 1024)


def _optimize_pdf(path: Path) -> None:
    """Losslessly compact the generated PDF before uploading it to Supabase."""
    original_size = path.stat().st_size
    optimized = path.with_name("translated.optimized.pdf")

    doc = pymupdf.open(path)
    try:
        doc.save(
            optimized,
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
        )
    finally:
        doc.close()

    optimized_size = optimized.stat().st_size
    if optimized_size < original_size:
        optimized.replace(path)
        print(
            f"PDF optimized: {_mb(original_size):.2f} MB -> "
            f"{_mb(optimized_size):.2f} MB",
            flush=True,
        )
    else:
        optimized.unlink(missing_ok=True)
        print(f"PDF size: {_mb(original_size):.2f} MB", flush=True)


def _is_transient_error(message: str) -> bool:
    text = message.lower()
    return (
        "transient_gemini_error:" in text
        or "transient_google_translate_error:" in text
        or "cloud translation api http 429" in text
        or "cloud translation api http 500" in text
        or "cloud translation api http 502" in text
        or "cloud translation api http 503" in text
        or "cloud translation api http 504" in text
        or "temporarily unavailable" in text
        or "high demand" in text
        or "resource_exhausted" in text
        or "quota exceeded" in text
        or "all configured vision models" in text
        or "all configured translation models" in text
    )


def main(job_id: str) -> int:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SECRET_KEY"]
    db = create_client(url, key)

    progress_updates_enabled = True
    progress_floor = 0
    last_progress = -1
    last_progress_message = None
    cancel_event = threading.Event()
    monitor_stop = threading.Event()
    monitor_thread: threading.Thread | None = None
    checkpoint_state: dict = {}
    persist_checkpoint_fn = None

    def raise_if_cancelled() -> None:
        if cancel_event.is_set():
            raise ClientDisconnectedError(
                "CLIENT_DISCONNECTED: browser heartbeat expired; worker stopped safely."
            )

    set_cancel_check(raise_if_cancelled)

    def update_progress(percent: int, message: str) -> None:
        nonlocal progress_updates_enabled, progress_floor, last_progress, last_progress_message

        raise_if_cancelled()
        if not progress_updates_enabled:
            return

        percent = max(progress_floor, max(0, min(100, int(percent))))
        progress_floor = percent
        message = str(message)[:500]

        if percent == last_progress and message == last_progress_message:
            return

        try:
            (
                db.table("translation_jobs")
                .update(
                    {
                        "progress": percent,
                        "progress_message": message,
                        "progress_updated_at": now(),
                    }
                )
                .eq("id", job_id)
                .execute()
            )
            last_progress = percent
            last_progress_message = message
            print(f"Progress {percent}%: {message}", flush=True)
        except Exception as exc:
            # Backward compatible: an old database without the progress columns
            # must not make the actual PDF job fail.
            progress_updates_enabled = False
            print(
                "Live progress is disabled until "
                "supabase/UPDATE_EXISTING_SUPABASE.sql is applied: "
                f"{exc}",
                flush=True,
            )

    rows = (
        db.table("translation_jobs")
        .select("*")
        .eq("id", job_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        print(f"Job {job_id} not found", file=sys.stderr)
        return 2

    job = rows[0]
    progress_floor = max(0, min(100, int(job.get("progress") or 0)))

    # On-demand dispatch, browser retries, and the scheduled recovery path can
    # occasionally target the same job. Never process it twice.
    status = str(job.get("status", ""))
    if status == "done":
        print(f"Job {job_id} is already done; skipping duplicate dispatch.", flush=True)
        return 0
    if status == "processing":
        print(
            f"Job {job_id} is already processing; skipping duplicate dispatch.",
            flush=True,
        )
        return 0
    if status != "queued":
        print(
            f"Job {job_id} is in status {status!r}; not processing it.",
            flush=True,
        )
        return 0

    heartbeat_watch_enabled = "client_heartbeat_at" in job
    if heartbeat_watch_enabled and (
        job.get("client_active") is False
        or _heartbeat_is_stale(job.get("client_heartbeat_at"))
    ):
        db.table("translation_jobs").update(
            {
                "status": "paused",
                "client_active": False,
                "paused_at": now(),
                "progress_message": (
                    "브라우저 연결이 끊어져 작업을 잠시 멈췄습니다. "
                    "다시 접속하면 저장된 지점부터 이어서 진행합니다."
                ),
                "progress_updated_at": now(),
            }
        ).eq("id", job_id).execute()
        print(f"Job {job_id}: client heartbeat already stale; not starting worker.", flush=True)
        return 0

    db.table("translation_jobs").update(
        {
            "status": "processing",
            "started_at": now(),
            "error": None,
        }
    ).eq("id", job_id).execute()

    if heartbeat_watch_enabled:
        def monitor_client_heartbeat() -> None:
            monitor_db = create_client(url, key)
            last_db_success = time.monotonic()
            while not monitor_stop.wait(CLIENT_HEARTBEAT_POLL_SECONDS):
                try:
                    rows = (
                        monitor_db.table("translation_jobs")
                        .select("status,client_heartbeat_at,client_active")
                        .eq("id", job_id)
                        .limit(1)
                        .execute()
                        .data
                    )
                    last_db_success = time.monotonic()
                    if not rows:
                        cancel_event.set()
                        return
                    state = rows[0]
                    if state.get("client_active") is False:
                        cancel_event.set()
                        return
                    if _heartbeat_is_stale(state.get("client_heartbeat_at")):
                        cancel_event.set()
                        return
                    if str(state.get("status", "")) not in {"processing", "queued"}:
                        return
                except Exception as exc:
                    print(f"Heartbeat monitor DB check failed: {exc}", flush=True)
                    if time.monotonic() - last_db_success > CLIENT_HEARTBEAT_TIMEOUT_SECONDS:
                        cancel_event.set()
                        return

        monitor_thread = threading.Thread(
            target=monitor_client_heartbeat,
            name=f"client-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        monitor_thread.start()
    else:
        print(
            "Client heartbeat columns are unavailable; run the latest Supabase SQL "
            "to enable automatic worker stop on browser disconnect.",
            flush=True,
        )

    temp = Path(tempfile.mkdtemp(prefix=f"pdfjob-{job_id[:8]}-"))

    try:
        input_path = temp / "original.pdf"
        payload = db.storage.from_("documents").download(job["original_path"])
        input_path.write_bytes(payload)

        if input_path.read_bytes()[:4] != b"%PDF":
            raise RuntimeError("Uploaded file is not a valid PDF")

        print(
            f"Input PDF: {_mb(input_path.stat().st_size):.2f} MB",
            flush=True,
        )

        source_sha256 = _sha256(input_path)
        checkpoint_path = f"{job['user_id']}/{job_id}/state-v1.json.gz"
        checkpoint_state = _load_checkpoint(
            db,
            checkpoint_path,
            source_sha256=source_sha256,
            target_language=job["target_language"],
        )
        resumed = checkpoint_state is not None
        checkpoint_state = checkpoint_state or {}

        # A legacy requeued job may have a high visible progress value but no
        # persistent checkpoint. Only preserve the old percentage when a real
        # checkpoint exists.
        if not resumed:
            progress_floor = 0
            last_progress = -1
            update_progress(4, "문서를 불러오고 있습니다.")
            update_progress(6, "문서를 불러왔습니다 · 분석을 준비하고 있습니다.")
        else:
            try:
                db.table("translation_jobs").update(
                    {
                        "resume_count": int(job.get("resume_count") or 0) + 1,
                        "checkpoint_stage": str(checkpoint_state.get("stage", "saved"))[:80],
                        "checkpoint_updated_at": now(),
                    }
                ).eq("id", job_id).execute()
            except Exception as exc:
                print(f"Checkpoint metadata columns unavailable: {exc}", flush=True)

            update_progress(
                progress_floor,
                "이전 진행 지점을 불러왔습니다 · 완료된 부분은 건너뛰고 이어서 처리합니다.",
            )

        checkpoint_lock = threading.Lock()
        checkpoint_enabled = True

        def persist_checkpoint(state: dict) -> None:
            nonlocal checkpoint_enabled
            if not checkpoint_enabled:
                return

            with checkpoint_lock:
                try:
                    size = _save_checkpoint(
                        db,
                        checkpoint_path,
                        state,
                        source_sha256=source_sha256,
                        target_language=job["target_language"],
                    )
                    try:
                        db.table("translation_jobs").update(
                            {
                                "checkpoint_stage": str(state.get("stage", "saved"))[:80],
                                "checkpoint_updated_at": now(),
                            }
                        ).eq("id", job_id).execute()
                    except Exception:
                        pass
                    print(
                        f"Checkpoint saved: stage={state.get('stage')} "
                        f"size={_mb(size):.2f} MB",
                        flush=True,
                    )
                except Exception as exc:
                    # Translation should still be usable if the optional
                    # checkpoint bucket has not been installed yet.
                    checkpoint_enabled = False
                    print(
                        "Persistent checkpointing is unavailable until the latest "
                        f"Supabase SQL is applied: {exc}",
                        flush=True,
                    )

        persist_checkpoint_fn = persist_checkpoint
        output_path = temp / "translated.pdf"
        info = process_pdf(
            input_path,
            job["target_language"],
            temp / "work",
            output_path,
            max_pages=int(os.getenv("MAX_PAGES", "100")),
            progress_callback=update_progress,
            checkpoint_state=checkpoint_state,
            checkpoint_callback=persist_checkpoint,
        )

        update_progress(96, "생성된 PDF를 최적화하고 있습니다.")
        _optimize_pdf(output_path)
        output_size = output_path.stat().st_size

        if output_size > MAX_STORAGE_BYTES:
            raise RuntimeError(
                "Generated PDF is larger than the Supabase Free-plan "
                f"50 MB object limit ({_mb(output_size):.2f} MB)."
            )

        result_path = f"{job['user_id']}/{job_id}/translated.pdf"
        update_progress(98, "완성된 번역본을 저장하고 있습니다.")

        try:
            db.storage.from_("documents").upload(
                result_path,
                output_path.read_bytes(),
                {
                    "content-type": "application/pdf",
                    "upsert": "true",
                },
            )
        except Exception as upload_exc:
            text = str(upload_exc)
            if "413" in text or "Payload too large" in text:
                raise RuntimeError(
                    "Supabase Storage rejected the result PDF with HTTP 413. "
                    f"Generated file size is {_mb(output_size):.2f} MB. "
                    "Run the updated supabase/UPDATE_EXISTING_SUPABASE.sql "
                    "once so the documents bucket limit becomes 50 MB."
                ) from upload_exc
            raise

        done_payload = {
            "status": "done",
            "result_path": result_path,
            "pages": info["pages"],
            "translated_segments": info["translated_segments"],
            "finished_at": now(),
            "error": None,
        }
        if progress_updates_enabled:
            done_payload.update(
                {
                    "progress": 100,
                    "progress_message": "번역이 완료되었습니다.",
                    "progress_updated_at": now(),
                }
            )

        db.table("translation_jobs").update(done_payload).eq("id", job_id).execute()
        _delete_checkpoint(db, checkpoint_path)

        print(f"Completed {job_id}: {info}", flush=True)
        return 0

    except ClientDisconnectedError as exc:
        message = str(exc)
        try:
            if persist_checkpoint_fn is not None and checkpoint_state:
                persist_checkpoint_fn(checkpoint_state)
        except Exception as checkpoint_exc:
            print(
                f"Final disconnect checkpoint save failed: {checkpoint_exc}",
                flush=True,
            )

        pause_payload = {
            "status": "paused",
            "client_active": False,
            "paused_at": now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        if progress_updates_enabled:
            pause_payload.update(
                {
                    "progress_message": (
                        "브라우저 연결이 끊어져 작업을 잠시 멈췄습니다. "
                        "다시 접속하면 저장된 지점부터 이어서 진행합니다."
                    ),
                    "progress_updated_at": now(),
                }
            )
        db.table("translation_jobs").update(pause_payload).eq("id", job_id).execute()
        print(f"Client disconnected; paused {job_id} after checkpoint: {message}", flush=True)
        return 0

    except Exception as exc:
        message = str(exc)[-6000:]

        if _is_transient_error(message):
            # Keep the job in the queue. The next scheduled worker will try again.
            retry_payload = {
                "status": "queued",
                "error": None,
                "started_at": None,
                "finished_at": None,
            }
            if progress_updates_enabled:
                retry_payload.update(
                    {
                        "progress_message": (
                            "현재 요청이 많아 잠시 대기 중입니다. "
                            "저장된 진행 지점부터 자동으로 이어서 처리합니다."
                        ),
                        "progress_updated_at": now(),
                    }
                )

            db.table("translation_jobs").update(
                retry_payload
            ).eq("id", job_id).execute()
            print(
                f"Temporary translation-provider error; requeued {job_id}: {message}",
                file=sys.stderr,
            )
            return 0

        try:
            db.table("translation_jobs").update(
                {
                    "status": "failed",
                    "error": message,
                    "finished_at": now(),
                }
            ).eq("id", job_id).execute()
        finally:
            print(message, file=sys.stderr)
        return 1

    finally:
        monitor_stop.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=2.0)
        set_cancel_check(None)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m worker.run_job JOB_ID")
    raise SystemExit(main(sys.argv[1]))
