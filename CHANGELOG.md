# Changelog

## 1.2.0 (2026-09-07)

### Features

- **Chat v3 — Markdown rendering**: model replies render as Markdown
  (headers, bold/italic, lists, quotes, tables, inline/fenced code with
  `language-*` classes) via vendored marked 12.0.2 + DOMPurify 3.1.6
  (`js/md.js` bridge). Sanitization is mandatory in the pipeline: scripts,
  event handlers, `javascript:` URLs and embeds are stripped; the bridge
  degrades to escaped plain text if a library is missing. See
  `docs/CHAT-UI-V3.md`.

## 1.1.0 (2026-09-06)

### Features

- **Chat v2** — streaming chat UI: tokens render incrementally via the new
  `POST /api/chat/stream` SSE endpoint (Ollama NDJSON → SSE proxy),
  Send ↔ Stop toggle (AbortController), conversation history kept in state,
  generation cursor, in-transcript error rendering, completion metrics
  (model / tokens / duration), Clear button. `POST /api/chat/run` kept for
  compatibility. See `docs/CHAT-UI-V2.md`.

## 1.0.0 (2026-08-28)

Initial release.

### Features

- **Dashboard** — real-time GPU, Ollama, CPU overview with 10 history charts
- **Model Manager** — list, search, filter, sort, multi-select, batch delete, show (full details), copy, delete, pull (streaming progress), push, create (visual + raw Modelfile editor)
- **Running Models** — live list with VRAM, unload via `keep_alive=0`
- **Jobs** — pull/push/create/delete/copy/stop as async jobs, WebSocket progress, SQLite persistence, cancellation, restart recovery
- **System Monitoring** — CPU, RAM, swap, disk, network, top processes, Ollama process
- **GPU Page** — full NVML/nvidia-smi data, GPU processes, 16 GB VRAM tracking
- **V100 Fan Control** — reads fan_target/fan_pwm from `v100-fan.service` (ESP32 controller), never controls PWM
- **Ollama Console** — whitelisted commands, shell-injection protection, API translation + local CLI
- **LLM API** — manage OpenAI-compatible endpoints (Ollama, OmniRouter, +custom), status, latency, models, API key masking, SSRF protection
- **Logs** — Web UI, job, Ollama (systemd) logs — tail/search/filter
- **Settings** — configuration, password change, user management, audit log
- **Security** — PBKDF2 auth, sessions, CSRF, rate limiting, command whitelist, no-shell subprocess, audit log, API key masking, SSRF
- **WebSocket** — real-time snapshot push every 1s, job updates
- **History** — GPU/system metrics persisted to SQLite, pruned automatically, 10 chart types

### Architecture

- **Backend**: Python 3.12+, FastAPI, Uvicorn, psutil, httpx, aiosqlite, nvidia-ml-py
- **Frontend**: Vanilla JS SPA, Chart.js 4.4, hash router, WebSocket
- **Storage**: SQLite (settings, jobs, metrics, users, audit_log)
- **GPU**: NVML (primary) + nvidia-smi (fallback)
- **Fan**: `v100-fan.service` journalctl reader (systemd-journal group)
- **Deployment**: systemd unit + Docker compose

### Tests

- 83+ pytest tests (mock mode, no GPU required)
- jsdom frontend smoke test (all pages, auth, rendering)
- bash syntax validation