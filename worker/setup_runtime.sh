#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.TinyTeX/bin/x86_64-linux:$PATH"

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/pip install --disable-pip-version-check -r worker/requirements.txt
fi

if ! command -v xelatex >/dev/null 2>&1; then
  export TINYTEX_VERSION=2026.07
  export TINYTEX_INSTALLER=TinyTeX-1
  curl -fsSL https://yihui.org/tinytex/install-unx.sh | sh
  export PATH="$HOME/.TinyTeX/bin/x86_64-linux:$PATH"
fi

tlmgr install \
  kpfonts-otf \
  xetexko \
  cjk-ko \
  enumitem \
  geometry \
  graphics \
  amsmath \
  amsfonts

mkdir -p "$HOME/.cache/pdf-translator-fonts"

if [ ! -f "$HOME/.cache/pdf-translator-fonts/NanumMyeongjo-Regular.ttf" ]; then
  curl -fL --retry 3 \
    -o "$HOME/.cache/pdf-translator-fonts/NanumMyeongjo-Regular.ttf" \
    https://raw.githubusercontent.com/google/fonts/main/ofl/nanummyeongjo/NanumMyeongjo-Regular.ttf
fi

if [ ! -f "$HOME/.cache/pdf-translator-fonts/NanumMyeongjo-Bold.ttf" ]; then
  curl -fL --retry 3 \
    -o "$HOME/.cache/pdf-translator-fonts/NanumMyeongjo-Bold.ttf" \
    https://raw.githubusercontent.com/google/fonts/main/ofl/nanummyeongjo/NanumMyeongjo-Bold.ttf
fi

for font in \
  KpRoman-Regular.otf \
  KpRoman-Italic.otf \
  KpRoman-Bold.otf \
  KpRoman-BoldItalic.otf \
  KpSans-Regular.otf \
  KpSans-Italic.otf \
  KpSans-Bold.otf \
  KpSans-BoldItalic.otf
do
  src="$(kpsewhich "$font")"
  test -n "$src"
  cp -f "$src" "$HOME/.cache/pdf-translator-fonts/$font"
done

kpsewhich xetexko.sty >/dev/null
kpsewhich kolabels-utf.sty >/dev/null
kpsewhich geometry.sty >/dev/null
kpsewhich enumitem.sty >/dev/null
kpsewhich amsmath.sty >/dev/null
kpsewhich amssymb.sty >/dev/null

test -x ".venv/bin/python"
test -x "$HOME/.TinyTeX/bin/x86_64-linux/xelatex"
test -f "$HOME/.cache/pdf-translator-fonts/NanumMyeongjo-Regular.ttf"
test -f "$HOME/.cache/pdf-translator-fonts/NanumMyeongjo-Bold.ttf"
test -f "$HOME/.cache/pdf-translator-fonts/KpRoman-Regular.otf"
test -f "$HOME/.cache/pdf-translator-fonts/KpSans-Regular.otf"
