# PDF Translator v7.0

## Tables are real LaTeX

Tables are no longer cropped into images. Vision reconstructs logical rows,
cells, horizontal colspans, header rows, alignments, cell emphasis, and inline
math. Each text cell is translated independently with math protected, and the
final PDF renders the result with `tabularx` and `booktabs`.

A table is reconstructed semantically even when the source table itself is a
bitmap. Captions and notes remain separate document blocks.

## Figures preserve their source representation

The worker now inspects the source PDF object structure inside each figure bbox.

- Bitmap-only visual: keeps the original PNG/JPEG bytes when the bbox matches
  the embedded image; cropped bitmap regions remain raster at approximately
  their native source sampling density.
- Vector visual: clips the original source PDF region into a standalone PDF
  asset, preserving paths and PDF text as vector content.
- Mixed raster + vector visual: also uses a clipped PDF, preserving both kinds
  of source objects without flattening them into PNG.

Equations and tables are never treated as figure assets.

## Retained from v6.x

- 100-page limit
- homepage `대관령산양의 번역기`
- Unicode-name / math-transport repair
- source bold-weight preservation
- text-integrity validation
- Gemini rate-limit handling
- robust mathematical preflight and source-aware repair
