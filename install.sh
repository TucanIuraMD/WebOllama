#!/usr/bin/env bash
# WebOllama installer — sets up venv, deps, .env, optional systemd service
# Designed to run ON the Ollama server (same machine as Ollama + GPU).
set -euo pipefail

cd "$(dirname "$0")"

echo "==> WebOllama install (target: local Ollama server)"

# 1. Python
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found" >&2; exit 1
fi
echo "    python3: $(python3 --version)"

# 2. venv
if [ ! -x venv/bin/python ]; then
  echo "==> Creating virtualenv"
  python3 -m venv venv
fi

# 3. deps
echo "==> Installing dependencies"
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q
echo "    deps installed"

# 4. .env
if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from .env.example"
fi

# 5. data/logs dirs
mkdir -p data logs

# 6. optional systemd
if [ "${1:-}" = "--systemd" ]; then
  if command -v systemctl >/dev/null 2>&1; then
    echo "==> Installing systemd service (ollama-web.service)"
    # dedicated user + video group (GPU access via /dev/nvidia*) +
    # systemd-journal group (read-only access to journald for v100-fan state)
    if ! id -u ollama-web >/dev/null 2>&1; then
      useradd -r -s /usr/sbin/nologin -d /opt/projects/WebOllama ollama-web
    fi
    usermod -a -G video,systemd-journal ollama-web 2>/dev/null || true
    chown -R ollama-web:ollama-web "$PWD"
    sed "s|/opt/projects/WebOllama|$PWD|g" ollama-web.service > /etc/systemd/system/ollama-web.service
    systemctl daemon-reload
    systemctl enable ollama-web
    systemctl restart ollama-web || true
    echo "    service installed & started: systemctl status ollama-web"
  else
    echo "WARN: systemctl not found, skipping systemd"
  fi
fi

echo "==> Done. Start with ./start.sh"
echo "    Web UI: http://<this-host>:8080  (default login admin/changeme — change it!)"
