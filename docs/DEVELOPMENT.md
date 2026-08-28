# Development

## Setup

```bash
git clone <repo> /opt/ollama-web
cd /opt/ollama-web
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio    # for running tests
```

## Project structure

```
webui/
├── main.py               # FastAPI app, lifespan (start/stop services)
├── config.py             # .env / environment-variable settings
├── db.py                 # SQLite — settings, jobs, metrics, users, audit_log
├── auth.py               # PBKDF2 password hashing, session management
├── security.py           # rate limiting, CSRF token helpers
├── ollama_client.py      # async Ollama HTTP API client (httpx)
├── gpu_collector.py      # NVML + nvidia-smi GPU collector
├── v100_fan.py           # v100-fan.service state reader (journalctl)
├── sys_collector.py      # psutil system monitoring
├── jobs.py               # async job manager (SQLite-persisted, cancellable)
├── ollama_console.py     # whitelisted Ollama CLI parser/runner
├── llm_api.py            # OpenAI-compatible LLM provider abstraction
├── realtime.py           # WebSocket snapshot + metric persistence loops
├── mock/                 # mock collectors for tests / dev only
├── routers/              # FastAPI route handlers
│   ├── auth.py, status.py, ollama.py, jobs.py, console.py
│   ├── llm.py, logs.py, settings.py, audit.py, ws.py
└── static/               # SPA frontend
    ├── index.html
    ├── css/app.css
    └── js/
        ├── api.js, ws.js, app.js, charts.js
        └── pages/ (dashboard.js, models.js, running.js, jobs.js, …)
```

## Running during development

```bash
./start.sh                 # foreground (Ctrl-C to stop)
./start.sh --daemon        # background (use ./stop.sh)
```

## Tests

```bash
python -m pytest tests/ -q          # 83+ tests, mock mode, no GPU needed
python -m pytest tests/ -x -v       # stop on first failure, verbose
```

### Test structure

- `test_ollama_client.py` — mock transport, all API operations
- `test_gpu.py` — mock GPU collector
- `test_system.py` — real psutil
- `test_cli_whitelist.py` — command parser, shell injection
- `test_jobs.py` — job lifecycle, cancellation, persistence
- `test_auth.py` — password hashing, sessions, login
- `test_api.py` — REST endpoints via TestClient
- `test_llm_api.py` — LLM provider, masking, SSRF, manager
- `test_v100_fan.py` — fan parser, journal reader, permission diagnostics
- `test_realtime.py` — snapshot, metrics, history

### Frontend tests

```bash
node scripts/uitest/smoke.mjs       # jsdom SPA test (9 pages, auth, rendering)
```

## Coding conventions

- **Python**: PEP 8, type hints, async/await for I/O
- **JavaScript**: ES2021+, `const`/`let`, template literals, no transpilation
- **CSS**: custom properties, dark theme, responsive (PC + tablet + phone)
- **Shell**: `set -euo pipefail`

## Adding a new LLM provider

1. Create a subclass of `LLMProvider` in `webui/llm_api.py`
2. Register it in `make_provider()`
3. Add the default endpoint to `DEFAULT_ENDPOINTS` (or document for manual add)
4. No frontend changes needed — the UI renders providers generically

## Adding a new GPU metric

1. Add the field to the NVML `_collect_nvml` (use `_i`, `_s`, `_struct_attr`)
2. Add it to the nvidia-smi `_parse_smi_gpu` if applicable
3. Add it to the realtime metric persistence in `realtime.py`
4. Add it to the frontend (dashboard.js, gpu.js, charts.js)
5. Add tests