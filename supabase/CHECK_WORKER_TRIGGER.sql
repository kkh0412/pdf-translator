-- PDF Translator v6.8 worker diagnostics
-- Safe: does not print the actual GitHub token.

select
  extname,
  extversion
from pg_extension
where extname in ('supabase_vault', 'pg_net', 'pg_cron')
order by extname;

select
  exists (
    select 1
    from vault.decrypted_secrets
    where name = 'github_actions_token'
  ) as github_token_exists;

select
  tgname as trigger_name,
  case tgenabled
    when 'O' then 'enabled'
    when 'D' then 'disabled'
    else tgenabled::text
  end as trigger_status
from pg_trigger
where tgrelid = 'public.translation_jobs'::regclass
  and tgname = 'dispatch_pdf_translation_worker_after_insert';

select
  jobid,
  jobname,
  schedule,
  active
from cron.job
where jobname = 'pdf-translator-worker-recovery';

select
  id,
  status,
  progress,
  progress_message,
  dispatch_attempts,
  dispatch_request_id,
  dispatch_last_at,
  created_at,
  progress_updated_at
from public.translation_jobs
order by created_at desc
limit 8;

select
  j.id as translation_job_id,
  j.dispatch_request_id,
  r.status_code,
  r.error_msg,
  r.created
from public.translation_jobs j
left join net._http_response r
  on r.id = j.dispatch_request_id
where j.dispatch_request_id is not null
order by j.created_at desc
limit 8;

select
  pid,
  backend_type,
  state
from pg_stat_activity
where
  backend_type ilike '%pg_net%'
  or application_name ilike '%pg_cron%';
