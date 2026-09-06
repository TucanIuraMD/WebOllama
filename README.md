# WebOllama

**Production-ready Web UI for a local Ollama server with Tesla V100 monitoring, model management, Jobs, system monitoring and OpenAI-compatible LLM API.**

Runs directly on the AI server (same machine as Ollama + GPU) and has full local access to NVML / nvidia-smi / psutil / systemd / journalctl / the `ollama` binary. No SSH, no remote agents — everything works locally on the target machine.

## Screenshots

Real screenshots go in [`docs/images/`](docs/images/) and are referenced here. *(None added yet — they will be committed once captured from a live deployment.)*

---

## Features

### Dashboard
- Real-time GPU: name, utilization %, VRAM (used/total/%), temperature, power, fan control, clocks, PCIe
- Ollama: ONLINE/OFFLINE indicator, version, model count, loaded models
- Running models with VRAM
- CPU %, load average
- **History charts**: GPU util, VRAM, temperature, power, fan target, fan PWM, CPU, RAM, network, disk (1m/5m/15m/1h)

### Model Manager
- Table with search, family filter, multi-column sort, multi-select + batch delete
- **Show** — full details modal (details, modelfile, template, parameters, system, license, messages)
- **Copy**, **Delete** (with confirmation)
- **Pull** — streaming progress, speed, cancellation (as Jobs)
- **Push** — publish to a registry
- **Create** — visual + raw Modelfile editor
- **Running Models** — live list with VRAM + Unload (stop via `keep_alive=0`)

### Jobs
- All long operations (pull, push, create, delete, copy, stop) run as async Jobs
- Live progress over WebSocket, persisted to SQLite (survive restart), cancellable

### Agents
- Practical knowledge base: **which model actually works in which environment/agent**
  (Hermes, OpenCode, Claude, OpenWebUI — extensible at runtime)
- **Models × Agents matrix**: rows = live Ollama models (each variant separate),
  columns = agents, cell = human-verified assessment (status, capabilities, note, tested date)
- Statuses: `untested —` / `failed ❌` / `works ✓` / `good ✓✓` (no scores; **untested ≠ failed**;
  a new model appears as *untested*, never pre-marked)
- Click a cell → editor (status, capabilities, note; tested_at set automatically; reset to untested)
- Click a model → per-agent detail view; combined filters: search + agent + capability + status
- Deliberately **not a benchmark**: nothing is auto-scored and benchmark results never
  modify Agents assessments

### System Monitoring
- CPU (usage, load 1/5/15, cores, frequency), RAM, swap, disk, network per interface
- Top CPU / memory processes, Ollama process (PID, CPU, memory, status)

### GPU Page
- Full NVML / nvidia-smi data: utilization, VRAM, temperature, power draw/limit, fan target/PWM, clocks, P-State, PCIe, driver, CUDA
- GPU processes (PID, name, GPU memory) with Ollama highlighted

### V100 Fan Control
- Displays `fan_target` / `fan_pwm` from the existing **`v100-fan.service`** (ESP32 fan controller)
- WebOllama only **reads** the state — it never controls the PWM

### Ollama Console (Terminal)
- Whitelisted commands: `list`, `ps`, `show`, `pull`, `push`, `create`, `cp`, `rm`, `stop`, `version`, `help`
- API translation + local CLI, shell-injection protected, command history

### LLM API
- Manage OpenAI-compatible endpoints (Ollama, OmniRouter, +custom)
- Real-time status, latency, model lists, API key masking, SSRF protection

### Logs
- Web UI logs, job logs, Ollama (systemd) logs — tail/search/filter

### Security
- PBKDF2-HMAC-SHA256 auth, session tokens, CSRF, rate limiting, command whitelist, subprocess without shell, audit log, API key masking, SSRF protection

### Everything real
- All data comes from NVML, nvidia-smi, psutil, Linux, the Ollama API and `v100-fan.service` — **no fake statistics, no mock data in production**

---

## Requirements

- Linux (tested on Ubuntu 24.04) with **systemd**
- Python 3.10+ (3.12 recommended)
- Ollama (local, on `http://127.0.0.1:11434`)
- NVIDIA GPU with driver + `nvidia-smi` (NVML via `nvidia-ml-py`)
- Optional: `v100-fan.service` for fan control display
- Optional: Docker + NVIDIA Container Toolkit for containerized GPU monitoring

## Quick Start

```bash
git clone <your-webollama-repo> /opt/projects/WebOllama
cd /opt/projects/WebOllama
./install.sh                # venv + deps + .env
./start.sh                  # Web UI on 0.0.0.0:8080
```

Open **http://<server>:8080** from any device (default login `admin` / `changeme` — **change it!**).

## Installation

### systemd (recommended)

```bash
cd /opt/projects/WebOllama
./install.sh --systemd
systemctl status ollama-web
```

Creates a dedicated `ollama-web` user, grants `video` (GPU access) and `systemd-journal` (fan state logs) groups.

### Docker

```bash
cd /opt/projects/WebOllama
docker compose up -d
```

Uses host networking (`network_mode: host`) so `127.0.0.1:11434` works. For GPU monitoring inside the container, install the NVIDIA Container Toolkit and uncomment the `deploy.resources` section in `docker-compose.yml`. **Host deployment is preferred for full GPU stats.**

## Configuration

Copy `.env.example` to `.env` and edit:

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama API |
| `WEBUI_HOST` | `0.0.0.0` | Listen interface |
| `WEBUI_PORT` | `8080` | Web UI port |
| `REFRESH_INTERVAL` | `1.0` | WebSocket refresh (s) |
| `METRICS_INTERVAL` | `5.0` | Metric persistence (s) |
| `HISTORY_RETENTION` | `3600` | Metric retention (s) |
| `AUTH_ENABLED` | `true` | Enable authentication |
| `DEFAULT_ADMIN_USER` | `admin` | Default admin user |
| `DEFAULT_ADMIN_PASSWORD` | `changeme` | Default admin password (change!) |
| `GPU_ENABLED` | `true` | GPU monitoring |
| `V100_FAN_STATE_FILE` | *(empty)* | Optional fan state file; else journalctl |
| `V100_FAN_POLL_INTERVAL` | `5` | Fan state poll interval (s) |
| `SECRET_KEY` | *(auto)* | Session signing key |

## GPU Monitoring

- **NVML** (via `nvidia-ml-py`) is the primary source — utilization, VRAM, temperature, power, clocks, PCIe, processes
- **nvidia-smi** is the fallback
- Per-metric resilience: one failed metric becomes `null`; the collector keeps working
- Fan control comes from **`v100-fan.service`**, NOT from NVML fan speed

See [docs/GPU.md](docs/GPU.md) and [docs/V100_FAN.md](docs/V100_FAN.md).

## LLM API

Two OpenAI-compatible endpoints pre-configured on the server:

- **Ollama** — `http://192.168.80.22:11434/v1`
- **OmniRouter** — `http://192.168.80.22:20128/v1`

Status checks via `GET /models`, latency, model lists, per-endpoint API keys (masked), Add/Edit/Delete. See [docs/LLM_API.md](docs/LLM_API.md).

## Security

Authentication (PBKDF2 + sessions), CSRF, rate limiting, command whitelist, no-shell subprocess, audit log, delete confirmations, API key masking, SSRF restrictions. See [SECURITY.md](SECURITY.md).

## Testing

```bash
python -m pytest tests/ -q      # backend + API tests (mock mode, no GPU needed)
node scripts/uitest/smoke.mjs   # frontend jsdom smoke test (optional)
bash -n install.sh start.sh stop.sh
```

## Project Structure

```
webui/
├── main.py            # FastAPI app, lifespan
├── config.py          # settings (.env)
├── db.py              # SQLite (settings, jobs, metrics, users, audit_log)
├── auth.py            # PBKDF2 auth + sessions
├── security.py        # rate limiting, CSRF
├── ollama_client.py   # native Ollama HTTP API client
├── gpu_collector.py   # NVML + nvidia-smi collector
├── v100_fan.py        # v100-fan.service state reader
├── sys_collector.py   # psutil system monitoring
├── jobs.py            # async job manager (SQLite-persisted)
├── ollama_console.py  # whitelisted Ollama console
├── llm_api.py         # OpenAI-compatible endpoint providers
├── realtime.py        # WebSocket snapshot + metric persistence
├── routers/           # API endpoints
└── static/            # SPA frontend (vanilla JS + Chart.js)
tests/                 # pytest suite
docs/                  # documentation
```

## API

Full endpoint reference in [docs/API.md](docs/API.md). WebSocket at `/ws`.

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for real issues and fixes (NVML types, journal permissions, Docker GPU, fan state, etc.).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

Pending confirmation — a `LICENSE` file will be added once the project license is decided.
