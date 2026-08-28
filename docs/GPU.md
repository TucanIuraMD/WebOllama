# GPU Monitoring

## Sources

### NVML (primary)

The GPU collector uses NVML via `nvidia-ml-py` (the maintained fork of pynvml).
NVML is the **authoritative** source — it is fast and provides all metrics
through a single C library.

Fields collected per GPU:

| Field | NVML function | Unit |
|---|---|---|
| `name` | `nvmlDeviceGetName` | string |
| `driver_version` | `nvmlSystemGetDriverVersion` | string |
| `cuda_version` | `nvmlSystemGetCudaDriverVersion_v2` | "12.20" format |
| `temperature` | `nvmlDeviceGetTemperature` | °C |
| `utilization` | `nvmlDeviceGetUtilizationRates` → `.gpu` | % |
| `memory_utilization` | `nvmlDeviceGetUtilizationRates` → `.memory` | % |
| `vram_total` | `nvmlDeviceGetMemoryInfo` → `.total` | bytes |
| `vram_used` | `nvmlDeviceGetMemoryInfo` → `.used` | bytes |
| `vram_free` | `nvmlDeviceGetMemoryInfo` → `.free` | bytes |
| `power_draw` | `nvmlDeviceGetPowerUsage` | W (converted from mW) |
| `power_limit` | `nvmlDeviceGetEnforcedPowerLimit` | W (converted from mW) |
| `clocks` | `nvmlDeviceGetClockInfo` (SM) | MHz |
| `mem_clock` | `nvmlDeviceGetClockInfo` (MEM) | MHz |
| `pstate` | `nvmlDeviceGetPerformanceState` | 0–15 |
| `pcie_link` | `nvmlDeviceGetCurrPcieLinkGeneration` | int |
| `pcie_width` | `nvmlDeviceGetCurrPcieLinkWidth` | int |
| `pci_bus` | `nvmlDeviceGetPciInfo` | "0000:00:00.0" |
| Processes | `nvmlDeviceGetComputeRunningProcesses` + `nvmlSystemGetProcessName` | list |

### nvidia-smi (fallback)

If NVML is unavailable, the collector runs a single `nvidia-smi` subprocess per
sample (no per-second forks) with `--query-gpu` and `--query-compute-apps`.

### Per-metric resilience

If a single NVML call fails, that field becomes `null` in the snapshot. The
collector keeps working and logs the failure at most once per 30 seconds.

## Fan control

**The Tesla V100 fan is NOT monitored via NVML `nvmlDeviceGetFanSpeed()`.**

The V100 fan is governed by a separate `v100-fan.service` (ESP32 controller).
WebOllama reads the fan state from that service's journal.

See [V100_FAN.md](V100_FAN.md).

## GPU Processes

The GPU page shows running compute and graphics processes with:
- PID, process name, GPU memory usage
- `ollama` processes are highlighted with a badge

## History

GPU metrics are persisted to SQLite every `METRICS_INTERVAL` (5 s by default)
and pruned after `HISTORY_RETENTION` (1 hour by default).

History is available via `GET /api/history/gpu?minutes=60` and rendered as
charts on the Dashboard.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| GPU unavailable | No NVIDIA driver or NVML not installed | `nvidia-smi` should work; `pip install nvidia-ml-py` |
| `int' object has no attribute 'gpu'` | Old nvidia-ml-py bug (fixed) | Update to latest `nvidia-ml-py` |
| `nvidia-smi exited 2` | Invalid query field (fixed) | Already fixed in current code |
| `NVML_VALUE_NOT_AVAILABLE` sentinel | Power/clock not available on this GPU | Handled gracefully (`null`) |
| No GPU in Docker | NVIDIA Container Toolkit not installed | Install `nvidia-ctk` + uncomment compose section |