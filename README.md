# PDF Translator v6.14

## Fixed: `Šafránek` / `§afránek` false math-transport detection

The `§` character is used internally as a JSON-safe stand-in for a LaTeX
backslash, but it must never be treated as mathematical merely because it occurs
in prose.

v6.14:
- only command-shaped strings such as `§rho`, `§mathcal{...}`, `§frac{...}`
  are treated as leaked math transport;
- a literal `§` in ordinary prose is no longer automatically rejected;
- prose blocks are compared with overlapping source-PDF text hints before
  translation/rendering;
- high-confidence OCR/Vision glyph confusions are restored from the source text;
- for example `Dominik §afránek` + source hint `Dominik Šafránek` becomes
  `Dominik Šafránek`;
- author/affiliation/metadata prompts explicitly preserve Unicode spelling and
  diacritics from the PDF text layer;
- the Vision prompt now states that `§` is allowed only inside math fields and
  must never substitute for a Unicode prose character.

All v6.13 homepage changes and v6.12 translation/math/quota safeguards are retained.
