# PDF Translator v7.1

## Fixed: bare math transport embedded in prose

v7.0 could still abort a page when Vision returned a normal prose block with a
small inline formula mistakenly encoded inside the text part.

v7.1 repairs this deterministically before translation:

    §rho
      -> \rho

    §Pi_y
      -> \Pi_y

    C_{§mathcal{M}}
      -> C_{\mathcal{M}}

    D(§rho§Vert§gamma)
      -> D(\rho\Vert\gamma)

    S_{§mathcal M,§gamma}^{(j)}
      -> S_{\mathcal M,\gamma}^{(j)}

The recovered formula is stored behind the normal `[[MATH_n]]` placeholder, so
the translation model cannot alter it.

Sentence punctuation remains prose:
`§gamma,` protects only `\gamma`, while the comma remains outside the math span.
Internal punctuation such as the comma in `S_{\mathcal M,\gamma}` remains inside
the formula.

Only recognized mathematical transport commands are consumed. `§afránek` and
literal `§ 3` remain prose and retain the v6.14 source-text correction path.

The Vision prompt also explicitly forbids §-based math transport in text parts.

All v7.0 semantic LaTeX tables and source-native vector/raster figures remain.
