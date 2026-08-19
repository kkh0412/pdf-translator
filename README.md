# PDF Translator v6.13

## Homepage refresh
- Visible service name changed to `대관령산양의 번역기`.
- Hero copy, upload copy, result copy, and progress copy were rewritten in natural Korean.
- Backend implementation terms are removed from the normal user-facing interface.
- Language dropdown uses a custom down arrow positioned slightly inward from the left edge.
- The three feature explanations were rewritten around math fidelity, document appearance, and natural translation/reflow.
- The old first-demo/backend footer notice was removed.

## Translation pipeline
All v6.12 translation, math, quota, and integrity fixes are retained.

## Bold / font-weight fidelity
- Source PDF text hints now carry real bold/italic ratios.
- Actual source font weight overrides Vision guesses at block level.
- Fully bold unlabeled abstracts are restored as bold.
- Regular sans-serif headings are not forced bold.
- `SourceSubsection` no longer contains unconditional `\bfseries`.

## Text integrity
- Leaked Vision pseudo-markup such as `§math{§mathcal{M}}` is recovered into
  protected inline math before translation.
- Bare internal `§` transport markers are rejected instead of printed.
- Unicode controls/noncharacters/soft hyphens are stripped from prose.
- Korean output with syllable-by-syllable spacing is rejected and retried.
- Long English prose that remains untranslated in a Korean-target job is rejected.
- Final text-integrity validation runs before LaTeX generation.

## Gemini free-tier 429 handling
- Shared Vision/translation rate governor defaults to `GEMINI_SAFE_RPM=18`.
- Google Retry-After / "retry in ...s" delays are honored.
- All local Gemini threads share the same per-model cooldown.
- 429 no longer recursively splits translation batches.
- 429 no longer falls back to `preserving the original block`.
- If quota still cannot recover, the job stops explicitly instead of producing
  a mixed Korean/English PDF.
- Gemini 3.5 Flash-Lite remains the normal model; Gemini 3.6 Flash is not used
  as a quota-escape path.