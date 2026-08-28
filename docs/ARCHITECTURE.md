# Architecture

## Overview

```
Browser (any device)
        │  HTTP + WebSocket
        ▼
WebOllama :8080  (on the AI server, 192.168.80.22)
   ├── FastAPI + Uvicorn
   ├── SQLite (settings, jobs, metrics, users, audit_log)
   ├── GPU Collector  ────► NVML (pynvml / nvidia-ml-py)   [primary]
   │                        └──► nvidia-smi                 [fallback]
   ├── v100-fan reader ────► journalctl -u v100-fan.service (systemd-journal group)
   │                        └──► optional V100_FAN_STATE_FILE
   ├── System Collector ────► psutil (CPU/RAM/disk/network/processes)
   ├── Ollama Client ───────► Ollama :11434 (native HTTP API + local CLI)
   ├── LLM API Manager ─────► OpenAI-compatible /v1 endpoints
   │                          ├── Ollama     http://192.168.80.22:11434/v1
   │                          └── OmniRouter http://192.168.80.22:20128/v1
   └── Job Manager ─────────► async tasks persisted to SQLite

Ollama :11434
   └── local models (qwen3, deepseek, ornith, ...)

Tesla V100-SXM2-16GB
   ├── NVML statistics
   └── v100-fan.service (ESP32 fan controller)
       └── logs fan state: "V100: 54.0 C Target: 58% PWM: 35%"
```

## Component responsibilities

### GPU statistics — NVML
NVML (`nvidia-ml-py`) is the **authoritative** source for GPU statistics:
utilization, VRAM used/total, temperature, power draw/limit, clocks, P-State,
PCIe link, driver version, CUDA version, running processes.

If NVML is unavailable, the collector falls back to `nvidia-smi --query-gpu`.

The collector is per-metric resilient: a single failing metric becomes `null`
and does not break the snapshot. Failures are logged at most once per 30 s.

### Fan control — v100-fan.service
The Tesla V100 fan is governed by **`v100-fan.service`** (an ESP32-based
controller). WebOllama **only reads** the fan state from that service's journal:

```
V100: 54.0 C Target: 58% PWM: 35%
```

Parsed into:
- `fan_target` = 58
- `fan_pwm` = 35
- `temperature` (from the fan log line) = 54.0

**WebOllama NEVER controls the PWM.** It does not write to the fan controller
and does not expose a control interface.

**Important:** NVML `nvmlDeviceGetFanSpeed()` is **not** used as the V100 fan
value. The `fan` field is deliberately removed from the GPU API output; only
`fan_target` / `fan_pwm` from `v100-fan.service` are shown.

### System metrics — psutil
CPU, RAM, swap, disk, network and process data come from `psutil` on the host
where WebOllama runs. Because WebOllama runs on the same machine as the GPU and
Ollama, these are the real server metrics.

### Ollama — native HTTP API
All model operations use the native Ollama API (`/api/version`, `/api/tags`,
`/api/ps`, `/api/show`, `/api/pull`, `/api/push`, `/api/create`, `/api/copy`,
`/api/delete`, `/api/generate`). Streaming operations (pull/push/create) run as
Jobs. The Ollama Console prefers the local `ollama` binary when available and
`OLLAMA_URL` points at localhost.

### LLM API — OpenAI-compatible providers
`webui/llm_api.py` defines a provider abstraction:

```
LLMProvider
├── OllamaProvider      (http://host:11434/v1)
└── OmniRouterProvider  (http://host:20128/v1)
```

Status and model lists are fetched via `GET /v1/models`. New providers
(OpenRouter, vLLM, LiteLLM, OpenAI, ...) can be added as subclasses of
`LLMProvider` without frontend changes.

### Real-time data flow
1. `RealtimeService._realtime_loop()` builds a snapshot every `REFRESH_INTERVAL` (1 s).
2. The snapshot contains `gpu`, `cpu`, `ram`, `disk`, `network`, `ollama`, `llm`, `jobs`.
3. It is pushed to all WebSocket clients (`/ws`).
4. `RealtimeService._metrics_loop()` persists metrics to SQLite every `METRICS_INTERVAL` (5 s).
5. History charts read from `/api/history/{gpu|system}`.

### Jobs
Long-running operations are submitted to `JobManager`, which runs them as
asyncio tasks, persists state to SQLite on every progress tick, and broadcasts
updates over WebSocket. On restart, any non-terminal job is marked
"interrupted by restart".

## Technology stack

- **Backend**: Python 3.12+, FastAPI, Uvicorn, psutil, httpx, aiosqlite, nvidia-ml-py
- **Frontend**: Vanilla JS SPA, Chart.js 4.4, hash router, WebSocket
- **Storage**: SQLite (settings, jobs, metrics, users, audit_log)
- **GPU**: NVML (primary) + nvidia-smi (fallback)
- **Fan**: `v100-fan.service` journalctl reader
- **Deployment**: systemd unit + Docker compose
