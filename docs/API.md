# REST API Reference

## Authentication

All endpoints (except `/api/health`, `/api/auth/login`, `/api/auth/enabled`,
and static files) require authentication when `AUTH_ENABLED=true`.

Authenticate via:
- **Cookie**: `POST /api/auth/login` sets the `session` cookie.
- **Bearer token**: `Authorization: Bearer <token>` header.

## Endpoints

### Health

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | No | Server health check + Ollama online status |

### Authentication

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/auth/enabled` | No | Whether auth is enabled |
| POST | `/api/auth/login` | No | Login (username + password) → session cookie |
| POST | `/api/auth/logout` | Yes | Logout, clear session |
| GET | `/api/auth/me` | Yes | Current user info |
| POST | `/api/auth/password` | Yes | Change password |
| GET | `/api/auth/users` | Admin | List users |
| POST | `/api/auth/users` | Admin | Create user |

### Status / GPU / System

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/status` | Yes | Full real-time snapshot (GPU/CPU/RAM/disk/network/ollama/llm/jobs) |
| GET | `/api/gpu` | Yes | GPU data (NVML / nvidia-smi) |
| GET | `/api/gpu/processes` | Yes | GPU processes |
| GET | `/api/system` | Yes | System data (CPU/RAM/disk/network/processes) |
| GET | `/api/ollama/status` | Yes | Ollama online/offline status |
| GET | `/api/history/{kind}` | Yes | Metric history (`gpu` or `system`, `?minutes=60`) |

### Models

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/ollama/models` | Yes | List all models |
| GET | `/api/ollama/models/{name}` | Yes | Get model info |
| POST | `/api/ollama/models/{name}/show` | Yes | Full model details (modelfile, template, parameters, etc.) |
| POST | `/api/ollama/models/{name}/copy` | Yes | Copy model |
| DELETE | `/api/ollama/models/{name}` | Yes | Delete model |
| DELETE | `/api/ollama/models/{name}/stop` | Yes | Unload model from VRAM |
| POST | `/api/ollama/models/pull` | Yes | Pull model (returns job_id) |
| POST | `/api/ollama/models/push` | Yes | Push model (returns job_id) |
| POST | `/api/ollama/models/create` | Yes | Create model from Modelfile (returns job_id) |
| POST | `/api/ollama/models/batch-delete` | Yes | Batch delete multiple models |
| GET | `/api/ollama/running` | Yes | List models loaded in VRAM |

### Jobs

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/jobs` | Yes | List jobs |
| GET | `/api/jobs/{id}` | Yes | Get job details |
| POST | `/api/jobs/{id}/cancel` | Yes | Cancel a running job |

### Ollama Console

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/console/allowed` | Yes | List allowed commands |
| POST | `/api/console/run` | Yes | Execute a whitelisted Ollama command |

### Agents

Practical Models × Agents knowledge base (not a benchmark; no auto-scoring).

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/agents` | Yes | Configured agents + capabilities (DB rows, extensible) |
| GET | `/api/agents/capabilities` | Yes | Capability catalog |
| GET | `/api/agents/matrix` | Yes | Models (live from Ollama `/api/tags`) × agents + saved assessments; `ollama_online:false` degrades to saved data only |
| GET | `/api/agents/models/{model}` | Yes | One model: Ollama meta (if present) + all its assessments |
| POST | `/api/agents/agents` | Admin | Add a new agent/environment (e.g. Automation) |
| POST | `/api/agents/capabilities` | Admin | Add a new capability (e.g. OCR) |
| POST | `/api/agents/assessments` | Yes | Create/update one assessment: `{model, agent_id \| agent(slug), status: untested\|failed\|works\|good, note?, capabilities?, tested_at?}`; `tested_at` is server-generated, `untested` clears it |
| DELETE | `/api/agents/assessments/{id}` | Yes | Reset a cell to untested |

Model references are exact Ollama model names — variants such as
`deepseek-coder-v2` and `deepseek-coder-v2-tools-16k` stay separate rows.
Assessments exist independently of Ollama lifecycle: deleting a model in
Ollama never deletes its assessment history, and assessments can be edited
while Ollama is offline.

### LLM API

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/llm` | Yes | List LLM endpoints (masked keys, cached status) |
| GET | `/api/llm/{id}` | Yes | Get one endpoint |
| GET | `/api/llm/{id}/models` | Yes | List models from endpoint |
| POST | `/api/llm/{id}/check` | Yes | Trigger a live status check |
| POST | `/api/llm` | Admin | Add a new endpoint |
| PUT | `/api/llm/{id}` | Admin | Update an endpoint |
| DELETE | `/api/llm/{id}` | Admin | Delete an endpoint |

### Logs

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/logs/webui` | Yes | WebUI logs (`?tail=100&search=`) |
| GET | `/api/logs/job` | Yes | Job logs (`?job_id=xxx&tail=200`) |
| GET | `/api/logs/ollama` | Yes | Ollama systemd logs |

### Settings

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/settings` | Yes | Current settings + overrides |
| PUT | `/api/settings` | Admin | Update settings |

### Audit

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/audit` | Admin | Audit log entries |

## WebSocket

| Path | Description |
|---|---|
| `/ws` | Real-time snapshot push (every ~1s) + job updates |

The WebSocket connection receives JSON messages:

- `{"type": "snapshot", "ts": ..., "gpu": {...}, "cpu": {...}, "ollama": {...}, ...}`
- `{"type": "job_update", "job": {...}}`
- `{"type": "pong"}` (response to client `ping`)