"""Central configuration for WebOllama.

Settings are resolved from (in priority order):
1. Environment variables
2. .env file in the project root
3. Built-in defaults
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# ---- Ollama ----
OLLAMA_URL = _str("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
# Optional remote sysinfo endpoint (e.g. a host agent exposing /gpu, /system)
SYSINFO_URL = _str("SYSINFO_URL").rstrip("/")
# Host-agent serving CPU/GPU stats of the remote OLLAMA server
# (scripts/ollama22-host-agent.py). Falls back to SYSINFO_URL when unset.
PROCESSOR_URL = _str("PROCESSOR_URL", SYSINFO_URL).rstrip("/")

# ---- Server ----
WEBUI_HOST = _str("WEBUI_HOST", "0.0.0.0")
WEBUI_PORT = _int("WEBUI_PORT", 8080)

# ---- Sampling / history ----
REFRESH_INTERVAL = _float("REFRESH_INTERVAL", 1.0)   # WebSocket realtime snapshot cadence
METRICS_INTERVAL = _float("METRICS_INTERVAL", 5.0)   # persistence cadence to SQLite
HISTORY_RETENTION = _int("HISTORY_RETENTION", 3600)  # seconds of metrics retained

# ---- Auth ----
AUTH_ENABLED = _bool("AUTH_ENABLED", True)
DEFAULT_ADMIN_USER = _str("DEFAULT_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASSWORD = _str("DEFAULT_ADMIN_PASSWORD", "changeme")
SESSION_TTL_SECONDS = _int("SESSION_TTL_SECONDS", 60 * 60 * 24 * 7)

# ---- GPU ----
GPU_ENABLED = _bool("GPU_ENABLED", True)

# ---- V100 fan control (v100-fan.service) ----
# Optional state file; if empty the reader falls back to journalctl
V100_FAN_STATE_FILE = _str("V100_FAN_STATE_FILE", "")
# How often to poll journalctl (seconds) — the service logs only PWM changes
V100_FAN_POLL_INTERVAL = _float("V100_FAN_POLL_INTERVAL", 5.0)

# ---- Logging ----
LOG_LEVEL = _str("LOG_LEVEL", "INFO").upper()
LOG_FILE = _str("LOG_FILE", "logs/webui.log")

# ---- Database ----
DB_PATH = _str("DB_PATH", "data/webui.db")
if not os.path.isabs(DB_PATH):
    DB_PATH = str(BASE_DIR / DB_PATH)

SECRET_KEY = _str("SECRET_KEY", "") or secrets.token_hex(32)

# ---- Jobs ----
JOB_KEEP_DAYS = _int("JOB_KEEP_DAYS", 7)  # auto-prune finished jobs older than N days

# ---- Security ----
RATE_LIMIT_WINDOW = _int("RATE_LIMIT_WINDOW", 60)
RATE_LIMIT_MAX = _int("RATE_LIMIT_MAX", 60)          # general API calls per window per IP
RATE_LIMIT_MAX_DANGEROUS = _int("RATE_LIMIT_MAX_DANGEROUS", 10)  # pull/delete/copy/create/console per window


def settings_dict() -> dict:
    """Public settings exposed via /api/settings (never includes secrets)."""
    return {
        "ollama_url": OLLAMA_URL,
        "sysinfo_url": SYSINFO_URL,
        "webui_host": WEBUI_HOST,
        "webui_port": WEBUI_PORT,
        "refresh_interval": REFRESH_INTERVAL,
        "metrics_interval": METRICS_INTERVAL,
        "history_retention": HISTORY_RETENTION,
        "auth_enabled": AUTH_ENABLED,
        "gpu_enabled": GPU_ENABLED,
        "log_level": LOG_LEVEL,
        "db_path": DB_PATH,
        "version": __import__("webui.__version__", fromlist=["VERSION"]).VERSION,
    }
