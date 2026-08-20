-- Run this once in Supabase Dashboard > SQL Editor.
-- Safe to run repeatedly.

-- Supabase Free allows a global maximum object limit of 50 MB.
-- The original demo set this bucket to only 20 MB, which can reject
-- a translated result even when the uploaded source PDF was accepted.
update storage.buckets
set
  file_size_limit = 52428800,
  allowed_mime_types = array['application/pdf'],
  public = false
where id = 'documents';

-- Live progress fields for long-running PDF jobs.
alter table public.translation_jobs
add column if not exists progress smallint not null default 0;

alter table public.translation_jobs
add column if not exists progress_message text;

alter table public.translation_jobs
add column if not exists progress_updated_at timestamptz;

alter table public.translation_jobs
drop constraint if exists translation_jobs_progress_check;

alter table public.translation_jobs
add constraint translation_jobs_progress_check
check (progress between 0 and 100);

-- Keep the existing job policies.
alter table public.translation_jobs
drop constraint if exists translation_jobs_status_check;

alter table public.translation_jobs
add constraint translation_jobs_status_check
check (status in ('uploading','queued','processing','paused','done','failed'));

revoke all on table public.translation_jobs from anon, authenticated;
grant select, insert on table public.translation_jobs to authenticated;

drop policy if exists "users can read their own translation jobs"
on public.translation_jobs;

create policy "users can read their own translation jobs"
on public.translation_jobs
for select
to authenticated
using (auth.uid() = user_id);

drop policy if exists "users can create their own queued jobs"
on public.translation_jobs;

create policy "users can create their own queued jobs"
on public.translation_jobs
for insert
to authenticated
with check (
  auth.uid() = user_id
  and status = 'queued'
  and original_path = auth.uid()::text || '/' || id::text || '/original.pdf'
  and result_path is null
  and pages is null
  and translated_segments is null
  and progress = 0
  and progress_message is null
  and progress_updated_at is null
  and error is null
  and started_at is null
  and finished_at is null
);

drop policy if exists "users can upload their own document objects"
on storage.objects;

create policy "users can upload their own document objects"
on storage.objects
for insert
to authenticated
with check (
  bucket_id = 'documents'
  and (storage.foldername(name))[1] = auth.uid()::text
);

drop policy if exists "users can read their own document objects"
on storage.objects;

create policy "users can read their own document objects"
on storage.objects
for select
to authenticated
using (
  bucket_id = 'documents'
  and (storage.foldername(name))[1] = auth.uid()::text
);

-- ---------------------------------------------------------------------------
-- Immediate GitHub worker dispatch v6.8
-- ---------------------------------------------------------------------------
-- REQUIRED ONE-TIME SECRET:
-- Supabase Dashboard > Database > Vault
-- name  = github_actions_token
-- value = GitHub fine-grained PAT for kkh0412/pdf-translator
--         Repository permission: Actions -> Read and write
-- ---------------------------------------------------------------------------

-- Be explicit so a partially configured database does not silently miss the
-- dependencies required by the worker trigger.
create extension if not exists supabase_vault with schema vault;
create extension if not exists pg_net with schema extensions;
create extension if not exists pg_cron;

alter table public.translation_jobs
add column if not exists dispatch_attempts integer not null default 0;

alter table public.translation_jobs
add column if not exists dispatch_request_id bigint;

alter table public.translation_jobs
add column if not exists dispatch_last_at timestamptz;


-- Persistent checkpoint metadata. The actual compressed state lives in the
-- private translation-checkpoints Storage bucket.
alter table public.translation_jobs
add column if not exists checkpoint_stage text;

alter table public.translation_jobs
add column if not exists checkpoint_updated_at timestamptz;

alter table public.translation_jobs
add column if not exists resume_count integer not null default 0;

-- Browser heartbeat. A worker stops after the browser/client has not checked in
-- for the configured timeout, preserving its checkpoint instead of spending API
-- quota after the user has disconnected.
alter table public.translation_jobs
add column if not exists client_heartbeat_at timestamptz not null default now();

alter table public.translation_jobs
add column if not exists client_active boolean not null default true;

alter table public.translation_jobs
add column if not exists paused_at timestamptz;

insert into storage.buckets
  (id, name, public, file_size_limit, allowed_mime_types)
values
  ('translation-checkpoints', 'translation-checkpoints', false, 52428800,
   array['application/gzip'])
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

-- Dispatch exactly one queued job. This function can be called by both the
-- AFTER INSERT trigger and the recovery cron.
create or replace function public.dispatch_pdf_translation_worker(
  p_job_id uuid,
  p_reason text default 'insert'
)
returns bigint
language plpgsql
security definer
set search_path = public, vault, net, pg_temp
as $$
declare
  github_token text;
  request_id bigint;
  current_status text;
  current_client_active boolean;
  current_heartbeat timestamptz;
begin
  select status, client_active, client_heartbeat_at
  into current_status, current_client_active, current_heartbeat
  from public.translation_jobs
  where id = p_job_id;

  if current_status is distinct from 'queued' then
    return null;
  end if;

  if current_client_active is false
     or current_heartbeat is null
     or current_heartbeat < now() - interval '45 seconds' then
    update public.translation_jobs
    set
      status = 'paused',
      paused_at = now(),
      client_active = false,
      progress_message =
        '브라우저 연결이 끊어져 작업을 잠시 멈췄습니다. 다시 접속하면 저장된 지점부터 이어서 진행합니다.',
      progress_updated_at = now()
    where id = p_job_id;
    return null;
  end if;

  select decrypted_secret
  into github_token
  from vault.decrypted_secrets
  where name = 'github_actions_token'
  order by created_at desc
  limit 1;

  if github_token is null or length(github_token) < 10 then
    update public.translation_jobs
    set
      progress = greatest(progress, 1),
      progress_message =
        '번역 서비스를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.',
      progress_updated_at = now()
    where id = p_job_id;

    return null;
  end if;

  begin
    select net.http_post(
      url := 'https://api.github.com/repos/kkh0412/pdf-translator/actions/workflows/process-pdf.yml/dispatches',
      headers := jsonb_build_object(
        'Accept', 'application/vnd.github+json',
        'Authorization', 'Bearer ' || github_token,
        'X-GitHub-Api-Version', '2026-03-10',
        'Content-Type', 'application/json',
        'User-Agent', 'pdf-translator-supabase-db-trigger'
      ),
      body := jsonb_build_object(
        'ref', 'main',
        'inputs', jsonb_build_object(
          'job_id', p_job_id::text
        )
      ),
      timeout_milliseconds := 8000
    )
    into request_id;

    update public.translation_jobs
    set
      dispatch_attempts = dispatch_attempts + 1,
      dispatch_request_id = request_id,
      dispatch_last_at = now(),
      progress = greatest(progress, 2),
      progress_message =
        case
          when progress >= 4 or checkpoint_stage is not null
            then '이전 진행 지점부터 이어갈 준비를 하고 있습니다.'
          else '번역을 시작할 준비를 하고 있습니다.'
        end,
      progress_updated_at = now()
    where id = p_job_id;

    return request_id;

  exception
    when others then
      update public.translation_jobs
      set
        dispatch_attempts = dispatch_attempts + 1,
        dispatch_last_at = now(),
        progress = greatest(progress, 1),
        progress_message =
          '번역 요청을 준비하지 못했습니다. 잠시 후 다시 시도합니다.',
        progress_updated_at = now()
      where id = p_job_id;

      return null;
  end;
end;
$$;

revoke all on function public.dispatch_pdf_translation_worker(uuid, text)
from public;

-- Authenticated browser heartbeat / resume signal. The browser cannot update
-- arbitrary job fields directly; this narrowly scoped SECURITY DEFINER RPC
-- verifies ownership first.
create or replace function public.pdf_translation_client_signal(
  p_job_id uuid,
  p_action text default 'heartbeat'
)
returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  current_user uuid;
  current_owner uuid;
  current_status text;
  normalized_action text;
begin
  current_user := auth.uid();
  if current_user is null then
    raise exception 'authentication required';
  end if;

  normalized_action := lower(coalesce(p_action, 'heartbeat'));

  select user_id, status
  into current_owner, current_status
  from public.translation_jobs
  where id = p_job_id;

  if current_owner is null or current_owner <> current_user then
    raise exception 'job not found';
  end if;

  if normalized_action in ('heartbeat', 'resume') then
    update public.translation_jobs
    set
      client_heartbeat_at = now(),
      client_active = true
    where id = p_job_id;

    if current_status = 'paused' then
      update public.translation_jobs
      set
        status = 'queued',
        paused_at = null,
        started_at = null,
        finished_at = null,
        error = null,
        progress_message = '연결이 복구되어 저장된 진행 지점부터 이어서 진행합니다.',
        progress_updated_at = now(),
        dispatch_last_at = null
      where id = p_job_id;

      perform public.dispatch_pdf_translation_worker(p_job_id, 'client-resume');
      return 'queued';
    end if;

    return current_status;
  end if;

  if normalized_action = 'disconnect' then
    update public.translation_jobs
    set client_active = false
    where id = p_job_id;
    return current_status;
  end if;

  raise exception 'unsupported client signal';
end;
$$;

revoke all on function public.pdf_translation_client_signal(uuid, text) from public;
grant execute on function public.pdf_translation_client_signal(uuid, text) to authenticated;

-- AFTER INSERT trigger wrapper.
create or replace function public.dispatch_pdf_translation_worker_after_insert()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  perform public.dispatch_pdf_translation_worker(new.id, 'insert');
  return new;
end;
$$;

revoke all on function public.dispatch_pdf_translation_worker_after_insert()
from public;

drop trigger if exists dispatch_pdf_translation_worker_after_insert
on public.translation_jobs;

create trigger dispatch_pdf_translation_worker_after_insert
after insert on public.translation_jobs
for each row
execute function public.dispatch_pdf_translation_worker_after_insert();

-- Read completed pg_net responses and turn GitHub/API errors into a message the
-- browser can show. GitHub workflow_dispatch can return 200 or 204 depending on
-- the REST API version; both are success.
create or replace function public.refresh_pdf_translation_dispatch_responses()
returns void
language plpgsql
security definer
set search_path = public, net, pg_temp
as $$
begin
  begin
    update public.translation_jobs as j
    set
      progress_message =
        case
          when r.status_code in (200, 204)
            then case when j.progress >= 4 or j.checkpoint_stage is not null
                 then '이전 진행 지점부터 이어갈 준비를 하고 있습니다.'
                 else '번역을 시작할 준비를 하고 있습니다.' end
          when r.status_code = 401
            then '번역 서비스를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.'
          when r.status_code = 403
            then '번역 서비스를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.'
          when r.status_code = 404
            then '번역 서비스를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.'
          when r.status_code is not null
            then '번역 요청을 처리하지 못했습니다. 잠시 후 다시 시도합니다.'
          when r.error_msg is not null
            then '네트워크 상태를 확인하며 번역 요청을 다시 준비하고 있습니다.'
          else j.progress_message
        end,
      progress = case
        when r.status_code in (200, 204) then greatest(j.progress, 3)
        else j.progress
      end,
      progress_updated_at = now()
    from net._http_response as r
    where
      j.status = 'queued'
      and j.dispatch_request_id = r.id
      and (
        r.status_code is not null
        or r.error_msg is not null
      );
  exception
    when undefined_table then
      -- pg_net is beta and internal response storage may vary by version.
      null;
  end;
end;
$$;

revoke all on function public.refresh_pdf_translation_dispatch_responses()
from public;

-- Recovery path that runs inside Supabase itself. If the initial INSERT trigger
-- or GitHub request was transiently lost, retry queued jobs after ~30 seconds.
create or replace function public.recover_queued_pdf_translation_workers()
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  rec record;
begin
  perform public.refresh_pdf_translation_dispatch_responses();

  for rec in
    select id
    from public.translation_jobs
    where
      status = 'queued'
      and client_active = true
      and client_heartbeat_at >= now() - interval '45 seconds'
      and created_at < now() - interval '20 seconds'
      and dispatch_attempts < 8
      and (
        dispatch_last_at is null
        or dispatch_last_at < now() - interval '45 seconds'
      )
    order by created_at
    limit 3
  loop
    perform public.dispatch_pdf_translation_worker(rec.id, 'cron-recovery');
  end loop;
end;
$$;

revoke all on function public.recover_queued_pdf_translation_workers()
from public;

-- Replace any previous recovery job with a sub-minute Supabase Cron job.
do $$
begin
  perform cron.unschedule('pdf-translator-worker-recovery');
exception
  when others then null;
end
$$;

-- Modern Supabase Cron supports interval syntax such as "30 seconds".
-- If the project's pg_cron is older, fall back to every minute.
do $$
begin
  perform cron.schedule(
    'pdf-translator-worker-recovery',
    '30 seconds',
    'select public.recover_queued_pdf_translation_workers();'
  );
exception
  when others then
    perform cron.schedule(
      'pdf-translator-worker-recovery',
      '* * * * *',
      'select public.recover_queued_pdf_translation_workers();'
    );
end
$$;

do $$
begin
  if not exists (
    select 1
    from vault.decrypted_secrets
    where name = 'github_actions_token'
  ) then
    raise notice
      'PDF Translator: github_actions_token is MISSING from Vault.';
  else
    raise notice
      'PDF Translator: Vault token found.';
  end if;

  if not exists (
    select 1
    from pg_trigger
    where tgrelid = 'public.translation_jobs'::regclass
      and tgname = 'dispatch_pdf_translation_worker_after_insert'
      and tgenabled <> 'D'
  ) then
    raise exception
      'PDF Translator: worker INSERT trigger was not installed.';
  end if;

  raise notice
    'PDF Translator: immediate trigger + recovery cron + client heartbeat installed.';
end
$$;
