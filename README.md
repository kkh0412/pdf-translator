# 대관령산양의 번역기 v7.4

## Persistent checkpoint / resume

GitHub-hosted runners are ephemeral, so local files alone cannot support a true
resume after a run ends. v7.4 stores a compact gzip JSON checkpoint in a private
Supabase Storage bucket (`translation-checkpoints`).

The checkpoint contains only semantic work, not disposable page images:
- document style / field / terminology strategy;
- completed Vision page structures;
- final semantic blocks and table structures;
- source-aware math repairs;
- completed translation blocks.

Figure assets are regenerated deterministically from the original PDF on the
new runner. Vector figures are clipped again as vector PDF and raster figures
are re-extracted as raster, so no AI call is repeated just to restore assets.

Resume granularity:
- Vision: after every completed page batch;
- structure: once all pages are reconstructed;
- math: after every successful formula repair, plus a final preflight-complete checkpoint;
- translation: after every completed translation batch.

Therefore a job interrupted after e.g. 129/211 translated blocks resumes with
the 130th unfinished block instead of retranslating the first 129.

A transient quota failure during source-aware math repair is now requeued as a
transient job rather than being converted into a permanent generic math error.
Previously repaired formulas are checkpointed and retained.

## User-facing progress messages

Normal progress text no longer exposes GitHub, worker, runner, Supabase, pg_net,
workflow, Vault, or token terminology. Both the latest SQL functions and the
browser contain a safety mapping so legacy technical messages are converted to
plain-language waiting/resume messages.

The visible progress percentage is monotonic across resume. A real checkpoint
keeps the previous percentage; a legacy requeued job without a checkpoint is
allowed to restart its progress honestly.

## One-time database update

Run the latest `supabase/UPDATE_EXISTING_SUPABASE.sql` once. It adds:
- `checkpoint_stage`
- `checkpoint_updated_at`
- `resume_count`
- private `translation-checkpoints` Storage bucket (50 MB/object, gzip only)
- user-friendly dispatch/recovery progress messages

All v7.3 quota-aware model failover and browser-local elapsed timer behavior,
v7.2 math fixes, v7.1 inline-math transport recovery, and v7.0 LaTeX table /
source-native vector-raster figure behavior are retained.
