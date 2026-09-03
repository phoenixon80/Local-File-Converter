#!/usr/bin/env bash
# Start the converter on http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"
PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=".venv/Scripts/python.exe"

if [ ! -x "$PYTHON" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
  PYTHON=".venv/bin/python"
  [ -x "$PYTHON" ] || PYTHON=".venv/Scripts/python.exe"
  "$PYTHON" -m pip install --quiet -r requirements.txt
fi

echo "Converter starting on http://127.0.0.1:$PORT"
exec "$PYTHON" -m uvicorn main:app --host 127.0.0.1 --port "$PORT"
