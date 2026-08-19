# PDF Translator v6.5 — on-demand worker

Normal flow:
1. Browser uploads PDF to Supabase.
2. Browser creates the queued job.
3. Browser immediately invokes authenticated Supabase Edge Function `trigger-worker`.
4. Edge Function dispatches GitHub Actions with the exact `job_id`.
5. GitHub Actions runs `python -m worker.run_job JOB_ID`.
6. Browser continues polling Supabase progress.

The 15-minute cron is only a recovery path.

One-time setup:
- Create one fine-grained GitHub PAT, restricted to `kkh0412/pdf-translator`,
  repository permission Actions: Read and write, Expiration: No expiration.
- Store it in Supabase Edge Function Secrets as `GH_ACTIONS_TOKEN`.
- Deploy the Edge Function `trigger-worker` using
  `supabase/functions/trigger-worker/index.ts`.

The GitHub token never appears in browser code or `docs/config.js`.
