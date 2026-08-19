# PDF Translator v6.8

## Worker startup hardening

A `queued / 0%` job means the database INSERT trigger did not run at all.
v6.8 makes the Supabase side self-contained:

- explicitly enables `supabase_vault`
- explicitly enables `pg_net`
- explicitly enables `pg_cron`
- installs an AFTER INSERT trigger
- immediately calls GitHub `workflow_dispatch(job_id)`
- records dispatch attempts/request id/timestamp in `translation_jobs`
- reads `pg_net` responses and exposes 401/403/404 errors in the web UI
- installs a Supabase-side recovery job every 30 seconds
  - falls back to every minute on older pg_cron versions
- the existing 15-minute GitHub schedule remains only a tertiary fallback

### One-time required setup

1. Supabase Dashboard -> Database -> Vault
2. Secret name: `github_actions_token`
3. Value: GitHub fine-grained PAT with repository `kkh0412/pdf-translator`,
   Actions -> Read and write
4. Supabase SQL Editor: run the ENTIRE latest
   `supabase/UPDATE_EXISTING_SUPABASE.sql`

Then run:
`supabase/CHECK_WORKER_TRIGGER.sql`

Expected:
- `github_token_exists = true`
- trigger status = enabled
- recovery cron active = true

For a job already stuck in queued state, run:
`supabase/RETRY_LATEST_QUEUED_JOB.sql`
