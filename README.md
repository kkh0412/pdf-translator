# PDF Translator v6.10

## Math pipeline overhaul

v6.10 no longer preflights all formulas in one XeLaTeX document.

Each inline/display formula is compiled independently (parallel workers), so one
bad formula cannot hide later errors and the whole 150+ formula document is not
recompiled from the beginning after every repair.

For every failing formula:
1. parse the XeLaTeX error;
2. apply a narrow deterministic repair when the failure is an undefined short
   text macro used only as a superscript/subscript, e.g. `^{\fn}` ->
   `^{\mathrm{fn}}`;
3. otherwise crop the exact source equation/paragraph from the original PDF and
   send that crop + compiler error + current transcription to Gemini;
4. reconstruct with standard LaTeX/amsmath/amssymb only;
5. recompile only that failed formula.

Every formula gets its own repair budget; the previous document-wide two-repair
limit has been removed.

The initial Vision transcription prompt now explicitly forbids invented custom
macros such as `\fn` and requires visible roman superscript/subscript labels to
be represented with `\mathrm{...}` / `\text{...}`.

The v6.9 aligned-equation renderer and automatic Supabase worker architecture
are retained.
