# 대관령산양의 번역기 v7.5

## Paragraph indentation
Each body paragraph is checked against the source PDF line geometry. First-line
x-position is compared with subsequent lines, so indented and non-indented
paragraphs can coexist as in the source. This deterministic pass also refreshes
older resumed checkpoints without a new Gemini request.

## Short display equations
Vision-supplied `equation_lines` no longer force an `aligned` environment. If
the complete formula is comfortably short for its local column, unnecessary
line breaks are discarded. Long formulas keep semantic/safe breaking.

## Sentence continuity around mathematics
Inline math placeholders are grammatical constituents of the surrounding
sentence. The prompt and validator prevent sentence boundaries from being
introduced merely around inline math. Read-only neighboring context is supplied
when prose continues across a standalone display equation. Older checkpoint
translations are revalidated and only failing blocks are retranslated.

All v7.4 checkpoint/resume and previous v7 table, figure, math, quota, and
frontend behavior is retained.
