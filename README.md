# PDF Translator v6.11

## Generic repeated-script repair

XeLaTeX rejects formulas such as:

    C_{\widetilde{\mathcal M}}_\gamma^{\dagger^T}
    X_a_b
    X^a^b
    X_a^b_c

with `Double subscript` / `Double superscript`.

v6.11 adds a recursive TeX-token normalizer before preflight. It preserves all
tokens and minimally groups the already-scripted atom:

    C_{\widetilde{\mathcal M}}_\gamma^{\dagger^T}
      -> {C_{\widetilde{\mathcal M}}}_\gamma^{\dagger^T}

    X_a_b
      -> {X_a}_b

    X_a^b_c
      -> {X_a^b}_c

It does not merge or delete indices, and it also works inside nested brace groups.

The source-aware Gemini repair prompt now explicitly prohibits repeated
subscripts/superscripts and tells the model to use the source crop to choose
between nested-script notation (A_{x_y}) and grouped-object notation
({A_x}_y).

All previous v6.10 source-aware, per-formula parallel preflight repairs and the
automatic Supabase worker architecture are retained.
