from __future__ import annotations

import argparse
import os

from supabase import create_client

from .run_job import main as run_one


def get_pending_ids(limit: int) -> list[str]:
    db = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SECRET_KEY'])
    rows = (
        db.table('translation_jobs')
        .select('id')
        .eq('status', 'queued')
        .order('created_at')
        .limit(limit)
        .execute()
        .data
    )
    return [row['id'] for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-jobs', type=int, default=3)
    args = parser.parse_args()

    job_ids = get_pending_ids(max(1, min(args.max_jobs, 10)))
    if not job_ids:
        print('No queued jobs.')
        return 0

    failures = 0
    for job_id in job_ids:
        failures += 1 if run_one(job_id) else 0
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
