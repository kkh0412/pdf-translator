# PDF Translator v6.3

- Fixes XeLaTeX failure caused by AI/source math commands such as `\\coloneq`.
- Normalizes common relation aliases and provides lightweight compatibility macros.
- Adds live Supabase progress (`progress`, `progress_message`) and a web progress bar.
- Uses Gemini 3.5 Flash-Lite first; Gemini 3.6 Flash remains a fallback.
- Reduces wasted translation retries: output-format/placeholder failures split the batch
  instead of repeatedly trying every model.
- Uses smaller translation batches for better structured-output reliability.
- Compile errors now include generated LaTeX source lines around the failure.

For an existing Supabase project, run `supabase/UPDATE_EXISTING_SUPABASE.sql`
once to enable live progress. The worker remains backward-compatible if the SQL
has not been run yet.
