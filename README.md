# PDF Translator v6.7

## Immediate worker: database-trigger architecture

Normal path:
browser -> INSERT translation_jobs ->
Postgres AFTER INSERT trigger ->
pg_net -> GitHub workflow_dispatch(job_id) ->
GitHub Actions.

The browser no longer depends on a deployed Edge Function, eliminating the
previous 0%-queued failure mode.

One-time setup:
1. Supabase Dashboard -> Database -> Vault
2. Add secret `github_actions_token`
3. Value = existing GitHub fine-grained PAT with Actions: write for
   `kkh0412/pdf-translator`
4. Run the entire latest `supabase/UPDATE_EXISTING_SUPABASE.sql`

Run `supabase/CHECK_WORKER_TRIGGER.sql` to diagnose token/trigger/pg_net status.

## Math preflight v2

- Actual formatting TABs are whitespace; they are no longer blindly converted
  into `\t`.
- Standalone `\t\t\t...` spacing garbage is removed.
- Unicode math glyphs such as `−`, `∈`, `≠`, `∞` are normalized to LaTeX.
- If a formula still fails preflight, only that formula gets a syntax-only
  Gemini repair, then preflight is retried.
