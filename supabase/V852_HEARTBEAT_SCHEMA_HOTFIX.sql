-- PDF Translator v8.5.2
-- Heartbeat / pause / resume schema hotfix.
-- Safe to run more than once.

begin;

-- ---------------------------------------------------------------------------
-- 1) Allow the paused state used when the browser disconnects.
-- ---------------------------------------------------------------------------
alter table public.translation_jobs
  drop constraint if exists translation_jobs_status_check;

alter table public.translation_jobs
  add constraint translation_jobs_status_check
  check (status in (
    'uploading',
    'queued',
    'processing',
    'paused',
    'done',
    'failed'
  ));

-- ---------------------------------------------------------------------------
-- 2) Heartbeat fields.
-- ---------------------------------------------------------------------------
alter table public.translation_jobs
  add column if not exists client_heartbeat_at timestamptz not null default now();

alter table public.translation_jobs
  add column if not exists client_active boolean not null default true;

alter table public.translation_jobs
  add column if not exists paused_at timestamptz;

-- Used by the resume path. Older installations may already have this.
alter table public.translation_jobs
  add column if not exists dispatch_last_at timestamptz;

-- These already exist in recent versions, but keeping the hotfix self-contained
-- avoids resume failures on older databases.
alter table public.translation_jobs
  add column if not exists progress_message text;

alter table public.translation_jobs
  add column if not exists progress_updated_at timestamptz;

-- Normalize pre-existing rows.
update public.translation_jobs
set
  client_heartbeat_at = coalesce(client_heartbeat_at, now()),
  client_active = coalesce(client_active, true)
where client_heartbeat_at is null
   or client_active is null;

-- ---------------------------------------------------------------------------
-- 3) Authenticated browser heartbeat / resume RPC.
-- ---------------------------------------------------------------------------
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
  v_client_user_id uuid;
  v_owner uuid;
  v_status text;
  v_action text;
begin
  v_client_user_id := auth.uid();

  if v_client_user_id is null then
    raise exception 'authentication required';
  end if;

  v_action := lower(trim(coalesce(p_action, 'heartbeat')));

  select user_id, status
    into v_owner, v_status
  from public.translation_jobs
  where id = p_job_id;

  if not found
     or v_owner is null
     or v_owner <> v_client_user_id then
    raise exception 'job not found';
  end if;

  if v_action in ('heartbeat', 'resume') then
    update public.translation_jobs
    set
      client_heartbeat_at = now(),
      client_active = true
    where id = p_job_id;

    if v_status = 'paused' then
      update public.translation_jobs
      set
        status = 'queued',
        paused_at = null,
        started_at = null,
        finished_at = null,
        error = null,
        progress_message =
          '연결이 복구되어 저장된 진행 지점부터 이어서 진행합니다.',
        progress_updated_at = now(),
        dispatch_last_at = null
      where id = p_job_id;

      -- Do not statically reference the dispatch function: this hotfix should
      -- still install correctly on a database where that function is absent.
      if to_regprocedure(
           'public.dispatch_pdf_translation_worker(uuid,text)'
         ) is not null then
        execute
          'select public.dispatch_pdf_translation_worker($1, $2)'
          using p_job_id, 'client-resume';
      end if;

      return 'queued';
    end if;

    return v_status;
  end if;

  if v_action = 'disconnect' then
    update public.translation_jobs
    set client_active = false
    where id = p_job_id;

    return v_status;
  end if;

  raise exception 'unsupported client signal: %', v_action;
end;
$$;

revoke all
on function public.pdf_translation_client_signal(uuid, text)
from public;

grant execute
on function public.pdf_translation_client_signal(uuid, text)
to authenticated;

commit;

-- ---------------------------------------------------------------------------
-- 4) Refresh Supabase/PostgREST schema cache.
-- ---------------------------------------------------------------------------
notify pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- 5) Verification.
-- Expected:
--   client_active       | boolean
--   client_heartbeat_at | timestamp with time zone
--   paused_at           | timestamp with time zone
--   heartbeat_rpc       | pdf_translation_client_signal(uuid,text)
--   status_check        | includes paused
-- ---------------------------------------------------------------------------
select
  column_name,
  data_type
from information_schema.columns
where table_schema = 'public'
  and table_name = 'translation_jobs'
  and column_name in (
    'client_heartbeat_at',
    'client_active',
    'paused_at'
  )
order by column_name;

select
  to_regprocedure(
    'public.pdf_translation_client_signal(uuid,text)'
  ) as heartbeat_rpc;

select
  pg_get_constraintdef(oid) as status_check
from pg_constraint
where conrelid = 'public.translation_jobs'::regclass
  and conname = 'translation_jobs_status_check';
