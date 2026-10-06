#!/usr/bin/env bash
# Repo Analysis Tool launcher: REST API + dashboard on http://127.0.0.1:8000
#   ./run.sh            -> port 8000
#   PORT=9000 ./run.sh  -> custom port
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/uvicorn ]; then
    echo "creating virtualenv and installing dependencies..."
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
fi

PORT="${PORT:-8000}"
exec .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port "$PORT" "$@"
