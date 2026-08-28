# Configuration

Configuration is read from environment variables, loaded from `.env` (created by
`./install.sh` from `.env.example`).

## Variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama API base URL |
| `SYSINFO_URL` | *(empty)* | Optional remote sysinfo endpoint (fallback for GPU data). Not needed for local deployment. |
| `WEBUI_HOST` | `0.0.0.0` | Bind interface for the Web UI |
| `WEBUI_PORT` | `8080` | Web UI port |
| `REFRESH_INTERVAL` | `1.0` | WebSocket snapshot interval (seconds) |
| `METRICS_INTERVAL` | `5.0` | Metric persistence interval (seconds) |
| `HISTORY_RETENTION` | `3600` | How long metrics are kept (seconds) |
| `AUTH_ENABLED` | `true` | Enable authentication |
| `DEFAULT_ADMIN_USER` | `admin` | Default admin username (created on first run) |
| `DEFAULT_ADMIN_PASSWORD` | `changeme` | Default admin password — **change immediately** |
| `GPU_ENABLED` | `true` | Enable GPU monitoring |
| `V100_FAN_STATE_FILE` | *(empty)* | Optional path to a fan state file. If set and exists, it is preferred over journalctl. |
| `V100_FAN_POLL_INTERVAL` | `5` | How often to poll journalctl for fan state (seconds) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_FILE` | `logs/webui.log` | Log file path |
| `DB_PATH` | `data/webui.db` | SQLite database path |
| `SECRET_KEY` | *(auto-generated)* | Session signing key. Set a fixed value to keep sessions across restarts. |

## Security notes

- `DEFAULT_ADMIN_PASSWORD` and `SECRET_KEY` are **secrets**. Do not commit the
  real `.env` to version control (it is in `.gitignore`).
- `.env.example` ships with safe placeholder values.
- Set `SECRET_KEY` to a fixed random value if you want sessions to survive a
  restart.

## LLM API endpoints

Pre-configured OpenAI-compatible endpoints (managed in the **LLM API** page, not
in `.env`):

| Name | Base URL |
|---|---|
| Ollama | `http://192.168.80.22:11434/v1` |
| OmniRouter | `http://192.168.80.22:20128/v1` |

They are stored in the SQLite `settings` table under the key `llm_endpoints`
and can be managed from the UI (Add / Edit / Delete). API keys are stored
there too and are masked in the UI/API.

## Runtime settings (Settings page)

Some settings can be changed at runtime via the **Settings** page and are stored
in SQLite:

- `ollama_url`, `sysinfo_url`
- `webui_host`, `webui_port`
- `refresh_interval`, `history_retention`
- `gpu_enabled`

Changes require a restart to fully take effect for the running server.
