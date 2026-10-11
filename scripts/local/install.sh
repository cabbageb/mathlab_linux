#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python_bin="${PYTHON_BIN:-python3}"
"$python_bin" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("MathLab Linux requires Python 3.11 or newer")
PY

"$python_bin" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
if [[ ! -f .env ]]; then
  cp .env.example .env
fi
echo "installed; edit config/provider.toml and .env before starting"
