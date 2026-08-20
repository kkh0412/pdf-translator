# PDF Translator v7.2

## Math: source-private differential macros

Physics/math papers often define private shorthand commands in their original
LaTeX preamble, e.g. `\dt` for the differential `dt`. Reconstructed formulas
do not have access to those private macros.

v7.2 deterministically expands an allow-list into portable standard LaTeX:

    \dt      -> \,\mathrm{d}t
    \dx      -> \,\mathrm{d}x
    \dy      -> \,\mathrm{d}y
    \dz      -> \,\mathrm{d}z
    \dr      -> \,\mathrm{d}r
    \dtheta  -> \,\mathrm{d}\theta
    \dphi    -> \,\mathrm{d}\phi
    ...

The allow-list avoids corrupting standard commands such as `\det`, `\dfrac`,
`\dagger`, etc.

The exact reported Eq. (27) regression is included in the test suite.

## Equation labels and paired definitions

Vision sometimes returns an equation number already wrapped as `(27)`. v7.2
normalizes this before amsmath `\tag`, so it becomes `\tag{27}` rather than
`\tag{(27)}`.

Displays of the form

    A := ... \quad \text{and} \quad B := ...

are split at the semantic `and` boundary before generic line breaking.

## Gemini fallback quota behavior

The v7.1 single-block quality fallback could still terminate immediately on a
429 after merely setting a cooldown.

v7.2 uses the same Retry-After loop for fallback models:
- read Google's Retry-After / retry-in delay;
- apply the shared model cooldown;
- retry the same fallback model;
- stop only after the configured retry budget is genuinely exhausted.

It still never inserts untranslated source prose into a "successful" PDF.

All v7.1 transport recovery and v7.0 LaTeX-table/vector-raster features remain.
