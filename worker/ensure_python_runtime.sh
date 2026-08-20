#!/usr/bin/env bash
set -euo pipefail

if [ ! -x ".venv/bin/python" ]; then
  echo "Prepared Python environment is missing; creating it now."
  python3 -m venv .venv
fi

runtime_ok() {
  .venv/bin/python - <<'PYCHECK'
import pymupdf
import supabase
import googletrans
print(f"Python dependencies ready · googletrans={googletrans.__version__}")
PYCHECK
}

if runtime_ok; then
  exit 0
fi

echo "Cached Python environment is incomplete; synchronizing requirements now."
.venv/bin/python -m pip install --disable-pip-version-check -r worker/requirements.txt

# Hard verification: document processing must not start unless the no-key
# Google translation fallback is actually importable.
runtime_ok
