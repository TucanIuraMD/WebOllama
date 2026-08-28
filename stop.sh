#!/usr/bin/env bash
# Stop WebOllama (pid file based, with pgrep fallback)
set -euo pipefail
cd "$(dirname "$0")"

STOPPED=0
if [ -f data/webui.pid ]; then
  PID=$(cat data/webui.pid)
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    echo "Stopped PID $PID"
    STOPPED=1
  fi
  rm -f data/webui.pid
fi

# fallback: kill any process running the webui.main module
PIDS=$(pgrep -f "python -m webui.main" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  for P in $PIDS; do
    kill "$P" 2>/dev/null || true
  done
  echo "Stopped (pgrep fallback): $PIDS"
  STOPPED=1
fi

if [ "$STOPPED" = "0" ]; then
  echo "No WebOllama process found — nothing to stop"
fi
