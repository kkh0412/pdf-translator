#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.TinyTeX/bin/x86_64-linux:$PATH"

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

# A restored GitHub Actions cache can contain an older virtualenv. Always
# synchronize requirements with the current repository before using it.
.venv/bin/python -m pip install --disable-pip-version-check --upgrade pip
.venv/bin/python -m pip install --disable-pip-version-check -r worker/requirements.txt

# Fail during runtime preparation rather than much later, after a Gemini 429.
.venv/bin/python - <<'PYVERIFY'
import pymupdf
import supabase
import googletrans
print(f"Python runtime dependencies OK · googletrans={googletrans.__version__}")
PYVERIFY

if ! command -v xelatex >/dev/null 2>&1; then
  export TINYTEX_VERSION=2026.07
  export TINYTEX_INSTALLER=TinyTeX-1
  curl -fsSL https://yihui.org/tinytex/install-unx.sh | sh
  export PATH="$HOME/.TinyTeX/bin/x86_64-linux:$PATH"
fi

tlmgr install lm xetexko cjk-ko enumitem geometry graphics xcolor amsmath amsfonts tools booktabs

FONT_DIR="$HOME/.cache/pdf-translator-fonts"
mkdir -p "$FONT_DIR"

download_font () {
  local name="$1"
  local url="$2"
  if [ ! -f "$FONT_DIR/$name" ]; then
    curl -fL --retry 3 -o "$FONT_DIR/$name" "$url"
  fi
}

download_font NanumMyeongjo-Regular.ttf \
  https://raw.githubusercontent.com/google/fonts/main/ofl/nanummyeongjo/NanumMyeongjo-Regular.ttf
download_font NanumMyeongjo-Bold.ttf \
  https://raw.githubusercontent.com/google/fonts/main/ofl/nanummyeongjo/NanumMyeongjo-Bold.ttf
download_font NanumGothic-Regular.ttf \
  https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic/NanumGothic-Regular.ttf
download_font NanumGothic-Bold.ttf \
  https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic/NanumGothic-Bold.ttf

for font in \
  lmroman10-regular.otf lmroman10-bold.otf lmroman10-italic.otf lmroman10-bolditalic.otf \
  lmsans10-regular.otf lmsans10-bold.otf lmsans10-oblique.otf lmsans10-boldoblique.otf
do
  src="$(kpsewhich "$font")"
  test -n "$src"
  cp -f "$src" "$FONT_DIR/$font"
done

kpsewhich xetexko.sty >/dev/null
kpsewhich kolabels-utf.sty >/dev/null
kpsewhich amsmath.sty >/dev/null
kpsewhich multicol.sty >/dev/null
kpsewhich tabularx.sty >/dev/null
kpsewhich booktabs.sty >/dev/null
test -x ".venv/bin/python"
test -x "$HOME/.TinyTeX/bin/x86_64-linux/xelatex"
