# PROCESSOR agent — deploy on the Ollama host (192.168.80.22)

WebOllama (192.168.80.111) shows a **PROCESSOR** block on its Dashboard with
CPU/GPU/VRAM/temperature of the machine that actually runs Ollama —
**192.168.80.22**, not .111. To provide that data safely, the Ollama host runs
a minimal read-only agent: [`scripts/ollama22-host-agent.py`](../scripts/ollama22-host-agent.py).

Properties:

- stdlib only — no pip packages required on the Ollama host (uses `psutil` when
  available, otherwise `/proc/stat` + `/proc/loadavg`);
- GPU via `nvidia-smi --query-gpu=...` (no root needed); if `nvidia-smi` or the
  GPU is absent it answers `"gpus": []` instead of failing;
- **GET-only**, one endpoint (`/api/processor`), no secrets, no tokens, no
  write access, no impact on the Ollama API (separate process and port 8081);
- binds `127.0.0.1` by default — use `--bind` to expose it to the LAN.

WebOllama polls `PROCESSOR_URL` (default `http://192.168.80.22:8081`) every
few seconds. While the agent is not installed the Dashboard shows
`Processor information unavailable` — by design, without tracebacks.

## 1. Install the agent on 192.168.80.22 (any non-root user is fine)

```bash
# from .111 (any copy method works; no agent-side deps needed)
scp scripts/ollama22-host-agent.py user@192.168.80.22:~/ollama22-host-agent.py

# on .22 — try it manually first
python3 ~/ollama22-host-agent.py --bind 0.0.0.0 --port 8081
curl -s http://127.0.0.1:8081/api/processor | python3 -m json.tool
```

Expected payload:

```json
{
  "host": "ollama22",
  "cpu":  {"utilization": 12.5, "cores_physical": 8, "cores_logical": 16, "load": [1.1, 0.9, 0.8]},
  "gpus": [{"index": 0, "name": "Tesla V100-SXM2-16GB", "utilization": 74.0,
            "memory_used": 12400000000, "memory_total": 16160000000,
            "memory_utilization": 46.0, "temperature": 64.0}],
  "timestamp": "2026-09-06T14:00:00+0000"
}
```

## 2. Keep it running (choose one)

**user-level systemd (no root needed):**

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/ollama22-host-agent.service <<'UNIT'
[Unit]
Description=Read-only CPU/GPU stats agent for WebOllama PROCESSOR block

[Service]
ExecStart=%h/ollama22-host-agent.py --bind 0.0.0.0 --port 8081
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now ollama22-host-agent
# allow the unit to keep running after logout (one-time, needs one root command)
loginctl enable-linger $USER
```

**or plain nohup (no systemd):**

```bash
nohup python3 ~/ollama22-host-agent.py --bind 0.0.0.0 --port 8081 \
  >> ~/ollama22-host-agent.log 2>&1 &
```

## 3. Point WebOllama at the agent (on 192.168.80.111)

`.env` of WebOllama already contains (set during this task):

```text
PROCESSOR_URL=http://192.168.80.22:8081
```

Restart the WebOllama process/service, then verify from .111:

```bash
curl -s http://192.168.80.22:8081/api/processor | python3 -m json.tool
```

The Dashboard PROCESSOR block starts showing CPU/GPU/VRAM/temperature
automatically (3s polling), without a page reload.

## 4. Optional hardening (recommended)

- firewall: allow TCP/8081 **only from 192.168.80.111**:
  `ufw allow from 192.168.80.111 to any port 8081 proto tcp`
- keep the default loopback bind if WebOllama runs on the same host;
- the agent exposes no other endpoints and answers 405 to any POST.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `available: false, reason: remote host unreachable` | agent not running / firewall — run step 1, check `systemctl --user status ollama22-host-agent` |
| `gpus: []` on a host that has an NVIDIA GPU | `nvidia-smi` missing or not in PATH for the agent user — install driver userspace tools |
| data stays but marked `stale` | agent was briefly unreachable; WebOllama serves the last good payload for up to 60 s |
