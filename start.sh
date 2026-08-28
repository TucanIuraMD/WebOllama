#!/usr/bin/env bash
# WebOllama starter.
#   ./start.sh            — run in the foreground (Ctrl-C to stop)
#   ./start.sh --daemon   — run in the background, write data/webui.pid (use ./stop.sh)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
  echo "No venv — run ./install.sh first" >&2
  exit 1
fi

mkdir -p data logs

# default .env values
: "${WEBUI_HOST:=0.0.0.0}"
: "${WEBUI_PORT:=8080}"

if [ -f .env ]; then
  set -a; source .env; set +a
fi

echo "==> WebOllama on http://${WEBUI_HOST}:${WEBUI_PORT}"

if [ "${1:-}" = "--daemon" ]; then
  nohup ./venv/bin/python -m webui.main >> logs/run.log 2>&1 &
  echo $! > data/webui.pid
  echo "    started in background (pid $(cat data/webui.pid)); logs: logs/run.log"
  echo "    stop with ./stop.sh"
else
  exec ./venv/bin/python -m webui.main
fi
