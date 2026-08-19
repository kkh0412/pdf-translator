from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from supabase import create_client

from .pdf_pipeline import process_pdf


MAX_STORAGE_BYTES = 50 * 1024 * 1024


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
        or "temporarily unavailable" in text
        or "high demand" in text
    )


def main(job_id: str) -> int:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SECRET_KEY"]
    db = create_client(url, key)

    progress_updates_enabled = True
    last_progress = -1
    last_progress_message = None

    def update_progress(percent: int, message: str) -> None:
        nonlocal progress_updates_enabled, last_progress, last_progress_message

        if not progress_updates_enabled:
            return

        percent = max(0, min(100, int(percent)))
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

    db.table("translation_jobs").update(
        {
            "status": "processing",
            "started_at": now(),
            "error": None,
        }
    ).eq("id", job_id).execute()
    update_progress(4, "Worker가 작업을 시작했습니다. 원본 PDF를 불러오고 있습니다.")

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
        update_progress(6, "원본 PDF 로드 완료 · 문서 분석을 준비하고 있습니다.")

        output_path = temp / "translated.pdf"
        info = process_pdf(
            input_path,
            job["target_language"],
            temp / "work",
            output_path,
            max_pages=int(os.getenv("MAX_PAGES", "20")),
            progress_callback=update_progress,
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
        update_progress(98, "최종 PDF를 Supabase Storage에 업로드하고 있습니다.")

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
                    "progress_message": "번역 PDF가 준비되었습니다.",
                    "progress_updated_at": now(),
                }
            )

        db.table("translation_jobs").update(done_payload).eq("id", job_id).execute()

        print(f"Completed {job_id}: {info}", flush=True)
        return 0

    except Exception as exc:
        message = str(exc)[-6000:]

        if _is_transient_error(message):
            # Keep the job in the queue. The next scheduled worker will try again.
            db.table("translation_jobs").update(
                {
                    "status": "queued",
                    "error": (
                        "Gemini is temporarily busy. "
                        "The worker will retry automatically."
                    ),
                    "started_at": None,
                    "finished_at": None,
                }
            ).eq("id", job_id).execute()
            print(
                f"Temporary Gemini error; requeued {job_id}: {message}",
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
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m worker.run_job JOB_ID")
    raise SystemExit(main(sys.argv[1]))
