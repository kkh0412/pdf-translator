from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from supabase import create_client

from .pdf_pipeline import process_pdf


def now():
    return datetime.now(timezone.utc).isoformat()


def main(job_id: str) -> int:
    url = os.environ['SUPABASE_URL']
    key = os.environ['SUPABASE_SECRET_KEY']
    db = create_client(url, key)

    rows = db.table('translation_jobs').select('*').eq('id', job_id).limit(1).execute().data
    if not rows:
        print(f'Job {job_id} not found', file=sys.stderr)
        return 2
    job = rows[0]
    db.table('translation_jobs').update({'status': 'processing', 'started_at': now(), 'error': None}).eq('id', job_id).execute()

    temp = Path(tempfile.mkdtemp(prefix=f'pdfjob-{job_id[:8]}-'))
    try:
        input_path = temp / 'original.pdf'
        payload = db.storage.from_('documents').download(job['original_path'])
        input_path.write_bytes(payload)
        if not input_path.read_bytes()[:4] == b'%PDF':
            raise RuntimeError('Uploaded file is not a valid PDF')

        output_path = temp / 'translated.pdf'
        info = process_pdf(
            input_path,
            job['target_language'],
            temp / 'work',
            output_path,
            max_pages=int(os.getenv('MAX_PAGES', '20')),
        )
        result_path = f"{job['user_id']}/{job_id}/translated.pdf"
        db.storage.from_('documents').upload(
            result_path,
            output_path.read_bytes(),
            {'content-type': 'application/pdf', 'upsert': 'true'},
        )
        db.table('translation_jobs').update({
            'status': 'done',
            'result_path': result_path,
            'pages': info['pages'],
            'translated_segments': info['translated_segments'],
            'finished_at': now(),
        }).eq('id', job_id).execute()
        print(f'Completed {job_id}: {info}')
        return 0
    except Exception as exc:
        message = str(exc)[-6000:]
        try:
            db.table('translation_jobs').update({
                'status': 'failed',
                'error': message,
                'finished_at': now(),
            }).eq('id', job_id).execute()
        finally:
            print(message, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: python -m worker.run_job JOB_ID')
    raise SystemExit(main(sys.argv[1]))
