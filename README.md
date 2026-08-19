# PDF Translator v6.9

## Fixed: multi-line aligned equations in preflight

The previous preflight compiled raw display math inside `\[ ... \]`.
That is wrong for formulas containing alignment markers such as:

    a &:= b
    \\ &= c
    \\ &= d

The `&` character is valid only inside an alignment environment.

v6.9:
- preflights display equations through the exact final `_equation_tex()` renderer;
- therefore preflight and final PDF use the same `equation` / `aligned`
  environments, line breaking, tags, and width handling;
- normalizes Vision output `\ &=` to the real line break `\\ &=`;
- wraps even a one-row display in `aligned` whenever an `&` marker is present;
- adds `graphicx` to the preflight environment because the final renderer can
  use `\resizebox` for indivisible long equations;
- on failure prints the exact rendered LaTeX actually sent to XeLaTeX.

The v6.8 Supabase automatic-worker architecture is retained unchanged.
