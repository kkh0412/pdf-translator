# PDF Translator v6.4

## Terminology
- For Korean targets, only broadly standardized textbook/cross-field concepts are
  automatically translated.
- Niche or subfield-specific concept names default to English when a Korean
  rendering would be awkward or nonstandard.
- The pre-scan glossary records `policy=translate` or `policy=keep_english`.
- Translation batches must obey that policy consistently.

## Detailed progress
- Supabase progress is updated through document scan, page reconstruction,
  translation request starts, translation batch completions, LaTeX rendering,
  optimization, and final upload.
- Translation batches are smaller (10 blocks / ~4200 chars) so progress updates
  are more frequent and structured output is easier to validate.
- The browser polls every 1.5 seconds and also shows elapsed processing time.
- If the Supabase progress migration has not been applied, the UI explicitly says
  so instead of displaying a fake 5% forever.

Existing Supabase projects must run:
`supabase/UPDATE_EXISTING_SUPABASE.sql`

## Worker schedule
The automatic GitHub Actions schedule remains every 5 minutes. GitHub's scheduled
workflow mechanism does not support a shorter interval. `workflow_dispatch`
remains available for manual immediate tests.
