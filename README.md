# PDF Translator v6.6

## Fixed: JSON-damaged LaTeX

Math fields now use a transport character `§` instead of raw LaTeX backslashes
while inside Gemini structured JSON.

Example:
`§boldsymbol{§textsf{C}}^d`
becomes
`\boldsymbol{\textsf{C}}^d`
only after JSON parsing.

Legacy malformed responses are repaired before Unicode control-character
sanitation, so `\boldsymbol`, `\textsf`, `\frac`, `\rho`, etc. no longer lose
their command prefixes.

Math brace balance is checked during page reconstruction.

## Early formula preflight

Every inline/display formula is XeLaTeX-compiled before body translation starts.
A malformed formula therefore fails around the structure-analysis stage rather
than after the expensive full translation.

The on-demand worker architecture from v6.5 is unchanged.
