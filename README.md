# PDF Translator — fast GitHub Pages + Supabase demo

This version is optimized for short user wait time.

## What changed

- GitHub Pages remains the frontend.
- Supabase remains the database and private Storage backend.
- GitHub Actions still polls the Supabase queue every 5 minutes, so no expiring GitHub PAT is required.
- **XeLaTeX / TeX Live / Noto CJK apt installation has been removed.**
- PDF text is now overlaid directly on the original vector PDF with PyMuPDF.
- PyMuPDF's built-in multilingual text engine handles Korean/CJK text, so no 61 MB Noto CJK package download is needed per job.
- The queue check and processing now happen in one GitHub Actions job, avoiding a second fresh VM startup.
- Translation requests use larger batches to reduce API round trips.

## GitHub settings

Repository variable:

- `SUPABASE_URL` = `https://YOUR_PROJECT.supabase.co` (do not append `/rest/v1/`)

Repository secrets:

- `SUPABASE_SECRET_KEY`
- `OPENAI_API_KEY`

## Supabase

Enable anonymous sign-in and run the SQL file in `supabase/migrations/202608190001_init.sql` for a new project, or `supabase/UPDATE_EXISTING_SUPABASE.sql` for the existing demo project.

## GitHub Pages

Settings → Pages → Deploy from a branch → `main` → `/docs`.

## Remaining latency

Because this design avoids an expiring GitHub PAT, GitHub Actions discovers new queued jobs on its 5-minute schedule. Once a worker starts, there is no longer a TeX Live / CJK font installation phase. The main remaining time is GitHub runner startup, Python dependency setup, OpenAI translation, and PDF upload/download.
