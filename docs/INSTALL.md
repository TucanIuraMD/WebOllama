# Installation

Two deployment methods. **systemd (host) is recommended** — it gives WebOllama
direct access to NVML, `nvidia-smi` and the journal.

## Requirements

- Linux with **systemd** (Ubuntu 24.04 tested)
- Python 3.10+ (3.12 recommended)
- A local Ollama server on `http://127.0.0.1:11434`
- NVIDIA GPU with driver and `nvidia-smi` (for GPU monitoring)
- Port **8080** free (configurable via `WEBUI_PORT`)

## Method 1 — systemd (recommended)

```bash
# 1. Copy the project to the server
git clone <your-webollama-repo> /opt/ollama-web
cd /opt/ollama-web

# 2. Install (venv + dependencies + .env)
./install.sh

# 3. Install and start as a systemd service
./install.sh --systemd
systemctl status ollama-web
journalctl -u ollama-web -f
```

### What `--systemd` does

- Creates a dedicated system user `ollama-web`
- Adds `ollama-web` to the **`video`** group (access to `/dev/nvidia*` for NVML)
- Adds `ollama-web` to the **`systemd-journal`** group (read access to the
  journal so it can parse `v100-fan.service` state lines)
- Installs `ollama-web.service` and enables it
- Adjusts the `WorkingDirectory` in the unit to the actual install path

### Manual service control

```bash
systemctl start ollama-web
systemctl stop ollama-web
systemctl restart ollama-web
systemctl enable ollama-web   # start on boot
```

### If the service user already exists

```bash
usermod -aG video,systemd-journal ollama-web
systemctl restart ollama-web
```

## Method 2 — Docker

```bash
cd /opt/ollama-web
docker compose up -d
```

- Uses **host networking** (`network_mode: host`) so `127.0.0.1:11434` works.
- Web UI listens on `0.0.0.0:8080`.
- **GPU monitoring inside Docker requires the NVIDIA Container Toolkit** and the
  `deploy.resources.reservations.devices` section uncommented in
  `docker-compose.yml`.
- For the simplest and most reliable GPU stats, prefer the host deployment.

## Ports

| Port | Service |
|---|---|
| 8080 | WebOllama Web UI |
| 11434 | Ollama (existing) |
| 20128 | OmniRouter (existing) |

## Post-install

1. Change the default admin password (`admin` / `changeme`) in Settings.
2. Verify GPU detection:

```bash
nvidia-smi
sudo -u ollama-web journalctl -u v100-fan.service -n 5 --no-pager   # if v100-fan exists
```

3. Open the UI: `http://192.168.80.22:8080` (from your phone/desktop).
