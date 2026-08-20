-- Manually retry the newest queued PDF job once.
-- Useful immediately after installing the v6.8 worker trigger.

select public.dispatch_pdf_translation_worker(
  (
    select id
    from public.translation_jobs
    where status = 'queued'
    order by created_at desc
    limit 1
  ),
  'manual-retry'
);

select
  id,
  status,
  progress,
  progress_message,
  dispatch_attempts,
  dispatch_request_id,
  dispatch_last_at
from public.translation_jobs
order by created_at desc
limit 1;
