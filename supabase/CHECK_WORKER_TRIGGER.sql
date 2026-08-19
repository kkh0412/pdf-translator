-- Safe diagnostics: this does NOT print the GitHub token.

select
  exists (
    select 1
    from vault.decrypted_secrets
    where name = 'github_actions_token'
  ) as github_token_exists;

select
  tgname as trigger_name,
  tgenabled as enabled
from pg_trigger
where tgrelid = 'public.translation_jobs'::regclass
  and tgname = 'dispatch_pdf_translation_worker_after_insert';

select
  id,
  status,
  progress,
  progress_message,
  created_at,
  progress_updated_at
from public.translation_jobs
order by created_at desc
limit 5;

-- GitHub workflow_dispatch normally answers 204 No Content.
select
  id,
  status_code,
  error_msg,
  created
from net._http_response
order by created desc
limit 10;
