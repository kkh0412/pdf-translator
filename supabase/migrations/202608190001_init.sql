create extension if not exists pgcrypto;

create table if not exists public.translation_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'uploading' check (status in ('uploading','queued','processing','done','failed')),
  original_name text not null,
  target_language text not null,
  original_path text not null,
  result_path text,
  pages integer,
  translated_segments integer,
  error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

alter table public.translation_jobs enable row level security;

revoke all on table public.translation_jobs from anon, authenticated;
grant select on table public.translation_jobs to authenticated;

drop policy if exists "users can read their own translation jobs" on public.translation_jobs;
create policy "users can read their own translation jobs"
on public.translation_jobs
for select
to authenticated
using (auth.uid() = user_id);

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('documents', 'documents', false, 20971520, array['application/pdf'])
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "users can read their own document objects" on storage.objects;
create policy "users can read their own document objects"
on storage.objects
for select
to authenticated
using (
  bucket_id = 'documents'
  and (storage.foldername(name))[1] = auth.uid()::text
);
