-- 이전 버전 SQL을 이미 실행했다면, 이 파일 전체를 Supabase SQL Editor에서 한 번 실행하세요.
-- 새 프로젝트라면 migrations/202608190001_init.sql만 실행하면 됩니다.

alter table public.translation_jobs drop constraint if exists translation_jobs_status_check;
alter table public.translation_jobs add constraint translation_jobs_status_check
  check (status in ('uploading','queued','processing','done','failed'));

revoke all on table public.translation_jobs from anon, authenticated;
grant select, insert on table public.translation_jobs to authenticated;

drop policy if exists "users can read their own translation jobs" on public.translation_jobs;
create policy "users can read their own translation jobs"
on public.translation_jobs
for select
to authenticated
using (auth.uid() = user_id);

drop policy if exists "users can create their own queued jobs" on public.translation_jobs;
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
  and error is null
  and started_at is null
  and finished_at is null
);

drop policy if exists "users can upload their own document objects" on storage.objects;
create policy "users can upload their own document objects"
on storage.objects
for insert
to authenticated
with check (
  bucket_id = 'documents'
  and (storage.foldername(name))[1] = auth.uid()::text
);

drop policy if exists "users can read their own document objects" on storage.objects;
create policy "users can read their own document objects"
on storage.objects
for select
to authenticated
using (
  bucket_id = 'documents'
  and (storage.foldername(name))[1] = auth.uid()::text
);
