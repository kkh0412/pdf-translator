create extension if not exists pgcrypto;

create table if not exists public.translation_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'queued'
    check (status in ('queued','processing','done','failed')),
  original_name text not null,
  target_language text not null
    check (target_language in ('ko','en','ja','zh-CN','fr','de','es')),
  original_path text not null,
  result_path text,
  pages integer,
  translated_segments integer,
  progress smallint not null default 0
    check (progress between 0 and 100),
  progress_message text,
  progress_updated_at timestamptz,
  error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

alter table public.translation_jobs enable row level security;

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

insert into storage.buckets
  (id, name, public, file_size_limit, allowed_mime_types)
values
  ('documents', 'documents', false, 52428800, array['application/pdf'])
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

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
-- Immediate GitHub worker dispatch
-- ---------------------------------------------------------------------------
-- One-time prerequisite:
-- Supabase Dashboard > Database > Vault
-- Create a secret named exactly:
--   github_actions_token
-- Value = your existing GitHub fine-grained PAT with Actions: write for
--         kkh0412/pdf-translator.
--
-- The token never reaches browser JavaScript.
-- ---------------------------------------------------------------------------

create extension if not exists pg_net;

create or replace function public.dispatch_pdf_translation_worker()
returns trigger
language plpgsql
security definer
set search_path = public, vault, net, pg_temp
as $$
declare
  github_token text;
  request_id bigint;
begin
  if new.status <> 'queued' then
    return new;
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
      progress = 1,
      progress_message =
        '자동 worker 설정이 완료되지 않았습니다 · Supabase Vault에 github_actions_token을 추가하세요.',
      progress_updated_at = now()
    where id = new.id;

    return new;
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
          'job_id', new.id::text
        )
      ),
      timeout_milliseconds := 5000
    )
    into request_id;

    update public.translation_jobs
    set
      progress = 2,
      progress_message =
        'GitHub worker 실행 요청 전송 완료 · runner 시작을 기다리고 있습니다.',
      progress_updated_at = now()
    where id = new.id;

  exception
    when others then
      update public.translation_jobs
      set
        progress = 1,
        progress_message =
          '즉시 worker 요청에 실패했습니다 · 15분 복구 worker가 다시 확인합니다.',
        progress_updated_at = now()
      where id = new.id;
  end;

  return new;
end;
$$;

revoke all on function public.dispatch_pdf_translation_worker() from public;

drop trigger if exists dispatch_pdf_translation_worker_after_insert
on public.translation_jobs;

create trigger dispatch_pdf_translation_worker_after_insert
after insert on public.translation_jobs
for each row
execute function public.dispatch_pdf_translation_worker();

do $$
begin
  if not exists (
    select 1
    from vault.decrypted_secrets
    where name = 'github_actions_token'
  ) then
    raise notice
      'PDF Translator: Vault secret github_actions_token is missing. Add it before testing automatic worker startup.';
  else
    raise notice
      'PDF Translator: automatic GitHub worker dispatch trigger is configured.';
  end if;
end
$$;
