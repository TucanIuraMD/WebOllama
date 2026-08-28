# Troubleshooting

## GPU: `'int' object has no attribute 'gpu'`

**Symptom**: WebOllama logs show `NVML collection failed, falling back to nvidia-smi: 'int' object has no attribute 'gpu'` every second.

**Cause**: `nvmlDeviceGetTemperature()` returns an `int` (°C), but the code was
accessing it as if it returned a struct with `.gpu` — a bug in the NVML
collector. This has been fixed — `temperature` is now handled as an `int`.

**Fix**: Update to the latest version of WebOllama (the code has been fixed).

## GPU: `nvidia-smi exited 2`

**Symptom**: The nvidia-smi fallback fails with exit code 2.

**Cause**: An invalid query field `memory.clocks.throttle.reasons.active` was
included in the `--query-gpu` string. This field does not exist in `nvidia-smi`
and causes the command to fail.

**Fix**: Update to the latest version (the query has been fixed — the field was
removed).

## GPU: NVIDIA device not found in Docker

**Symptom**: WebOllama in Docker reports "no NVIDIA GPU detected".

**Cause**: The container does not have access to the NVIDIA driver or NVML.

**Fix**: Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
and uncomment the `deploy.resources.reservations.devices` section in
`docker-compose.yml`. For the simplest GPU monitoring, use the host deployment
(`./install.sh --systemd`).

## V100 fan: unavailable / no state lines in journal

**Symptom**: GPU page shows "Status: unavailable, Reason: no v100-fan state
lines in journal" or "permission denied reading journal".

**Cause**: The WebOllama user (`ollama-web`) is not in the `systemd-journal`
group and cannot read the `v100-fan.service` journal.

**Fix**:
```bash
usermod -aG systemd-journal ollama-web
systemctl restart ollama-web
```

**Verification**:
```bash
sudo -u ollama-web journalctl -u v100-fan.service -n 5 --no-pager
```

## V100 fan: Target ≠ PWM

**Expected**: Target and PWM are two different metrics and can differ (e.g.
Target 60%, PWM 47%). This is correct — the controller may be in a transition
state.

## Ollama: 502 / offline

**Symptom**: The UI shows "OLLAMA OFFLINE" and model operations return 502.

**Cause**: Ollama is not running or unreachable at `OLLAMA_URL`.

**Fix**:
```bash
systemctl status ollama         # check if Ollama is running
curl http://127.0.0.1:11434/api/version   # verify locally
```

## Authentication: cannot log in

**Symptom**: Login page shows "invalid credentials" for the correct password.

**Fix**: If this is the first run, the default credentials are `admin` /
`changeme` (set in `.env`). If the password was changed and forgotten, reset it
by setting `DEFAULT_ADMIN_PASSWORD` in `.env` and restarting (the user is
recreated only if it does not exist — delete the user from SQLite to force
recreation).

## Port conflict

**Symptom**: WebOllama fails to start because `8080` is already in use.

**Fix**: Change the port in `.env`:
```bash
WEBUI_PORT=8081
```

## WebSocket: no live updates

**Symptom**: Dashboard GPU values are not updating live.

**Fix**: Check the browser console for WebSocket errors. Ensure no proxy is
blocking WebSocket connections to `/ws`. The WebSocket reconnects automatically
with exponential backoff (max 30s).

## History charts: show 0 or empty

**Symptom**: Dashboard history charts show no data or flat line at 0.

**Cause**: The dashboard loads history once at render and then appends live
points. If the dashboard was rendered before metrics accumulated, the chart
would be empty. Live points are appended every second after that.

**Fix**: Reload the dashboard page after a few minutes of uptime. The initial
history load will then find data.