# V100 Fan Control

## Context

The Tesla V100 fan is **not** controlled by the NVIDIA driver's default fan
management. Instead, it is governed by a dedicated **`v100-fan.service`** that
runs on the same machine:

```
ExecStart=/usr/bin/python3 /home/iura/fan-control.py
User=iura
Group=dialout
```

This service uses an ESP32-based controller to manage the fan PWM directly.
It logs state lines to the systemd journal.

## WebOllama integration

WebOllama **only reads** the fan state. It never controls the PWM.

### Source

`v100_fan.py` reads the last state line from:

```
journalctl -u v100-fan.service -n 300 --no-pager
```

### Format

```
V100: 54.0 C Target: 58% PWM: 35%
```

Parsed fields:

| Field | Value | Source |
|---|---|---|
| `temperature` | `54.0` | First number after "V100:" |
| `fan_target` | `58` | Number after "Target:" |
| `fan_pwm` | `35` | Number after "PWM:" |

**Target and PWM are two different metrics.** They can differ (e.g. Target 58%,
PWM 37%). WebOllama preserves both.

### Fallback source

If the environment variable `V100_FAN_STATE_FILE` is set and points to a
readable file, the reader prefers that file over journalctl. The file can
contain either:
- A log-format line like `V100: 54.0 C Target: 58% PWM: 35%`
- JSON like `{"temperature": 54.0, "fan_target": 58, "fan_pwm": 35}`

### Permission

For `journalctl` to work, the WebOllama user (`ollama-web`) must be in the
**`systemd-journal`** group:

```bash
usermod -aG systemd-journal ollama-web
systemctl restart ollama-web
```

The `install.sh --systemd` command does this automatically.

## What WebOllama does NOT do

- **Does NOT** control the fan PWM
- **Does NOT** modify `/home/iura/fan-control.py`
- **Does NOT** modify `v100-fan.service`
- **Does NOT** use NVML `nvmlDeviceGetFanSpeed()` as the V100 fan value

## Display

The fan state appears in three places:

1. **Dashboard** — "Fan Control" card with Target / PWM
2. **GPU page** — "Fan Target" / "Fan PWM" metric tiles
3. **Settings** — "V100 Fan Control" read-only card with source info

If the fan state is unavailable, all fields show "—" with a "Status: unavailable"
indicator and a reason (e.g., systemd-journal group missing).