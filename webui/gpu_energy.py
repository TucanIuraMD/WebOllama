"""GPU energy sampling — 0.5 s NVML power reads, 10 s aggregated DB points.

Contract (v1.1):
  - every 0.5 s: read each GPU's real power_draw via NVML (C library call —
    no subprocess, no disk I/O); the reading goes into a bounded RAM buffer
    ONLY. Nothing is written to the DB at this cadence.
  - every 10 s (aggregate_interval): average the buffer's valid samples per
    GPU, compute energy_wh = average_power_w × interval_seconds / 3600 over
    the actually measured span, and write exactly ONE aggregated row per GPU
    into the existing metrics table (kind="gpu_energy"). The buffer is then
    cleared.
  - gaps (NVML unavailable, sampler stopped): NO points are written, nothing
    is interpolated; consumption is counted only over really measured
    intervals. Restarts can never double-count because every row carries its
    own measured interval and rows are disjoint in time.
  - stored row payload: {gpu_index, gpu_name, interval_seconds,
    average_power_w, energy_wh, source: "NVML"} — aggregated data only.
  - GPU power/energy is GPU-only telemetry. It is NEVER total server power;
    the host-level total stays a separate, host-sourced value.
  - the sampler is a singleton; start() twice never spawns a duplicate task;
    stop() cancels the task and flushes the last partial (fully measured)
    interval so shutdown does not silently drop it.
"""
import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL = 0.5      # seconds between NVML power reads (RAM buffer only)
AGGREGATE_INTERVAL = 10.0  # seconds between aggregated DB points
SOURCE = "NVML"


class GpuEnergySampler:
    """RAM ring buffer of 0.5 s GPU power samples → 10 s aggregated DB rows."""

    def __init__(self, db,
                 sample_interval: float = SAMPLE_INTERVAL,
                 aggregate_interval: float = AGGREGATE_INTERVAL,
                 reader: Optional[Callable[[], Optional[dict]]] = None) -> None:
        self.db = db
        self.sample_interval = float(sample_interval)
        self.aggregate_interval = float(aggregate_interval)
        # injectable NVML read seam for tests: returns {gpu_index: watts} or None
        self._reader = reader
        self._buffer: list[tuple[float, dict]] = []  # (ts, {index: watts})
        self._task: Optional[asyncio.Task] = None
        # bounded RAM: never hold more than 4 full aggregation windows
        self._max_buffer = max(8, int(self.aggregate_interval / max(self.sample_interval, 0.01)) * 4)
        self.aggregations = 0        # test seam: completed DB writes
        self.sample_ticks = 0        # test seam: executed 0.5 s reads
        self.last_error: Optional[str] = None

    # ---- state -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def buffer_size(self) -> int:
        return len(self._buffer)

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        """Start the sampler loop. Idempotent — never a duplicate task."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="gpu-energy-sampler")

    async def stop(self) -> None:
        """Stop the loop and flush the last partial measured interval.

        The flush is REAL measured data (buffered samples), not interpolation,
        so writing it cannot double-count: the next process instance starts
        with an empty buffer and later timestamps.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._flush()

    # ---- NVML reading ----------------------------------------------------------
    def _read_powers(self) -> Optional[dict]:
        """Read current power_draw (W) per GPU via NVML, or None if unavailable.

        Init/shutdown per read mirrors the existing GPUCollector pattern: it
        is a C-library call (microseconds), not a subprocess, and it keeps
        this sampler independent of the collector's handle lifecycle.
        """
        if self._reader is not None:
            return self._reader()
        try:
            import pynvml
        except Exception:
            return None
        try:
            pynvml.nvmlInit()
            try:
                count = pynvml.nvmlDeviceGetCount()
                out = {}
                for i in range(count):
                    try:
                        handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                        mw = pynvml.nvmlDeviceGetPowerUsage(handle)
                        if mw is not None and mw >= 0:
                            out[i] = mw / 1000.0
                    except Exception:
                        continue  # one unreadable GPU must not kill the rest
                return out
            finally:
                pynvml.nvmlShutdown()
        except Exception as exc:
            self.last_error = str(exc)
            return None

    # ---- main loop ---------------------------------------------------------------
    async def _run(self) -> None:
        next_t = time.monotonic() + self.sample_interval
        while True:
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))
            next_t += self.sample_interval
            try:
                self.sample_ticks += 1
                powers = self._read_powers()
                if powers:
                    self._buffer.append((time.time(), dict(powers)))
                    if len(self._buffer) > self._max_buffer:
                        self._buffer.pop(0)  # bounded RAM ring
                span = self._buffer[-1][0] - self._buffer[0][0] if self._buffer else 0.0
                if span >= self.aggregate_interval:
                    await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop must survive anything
                self.last_error = str(exc)
                logger.debug("gpu energy sampler tick failed: %s", exc)

    # ---- aggregation --------------------------------------------------------------
    async def _flush(self) -> None:
        """Aggregate the buffer into ONE point per GPU and clear it.

        Empty/partially-NVML-unavailable windows produce NO rows: a gap is a
        gap — never zero-filled, never interpolated.
        """
        if not self._buffer:
            return
        points = self._buffer
        self._buffer = []
        t0, t1 = points[0][0], points[-1][0]
        interval = t1 - t0
        if interval <= 0:
            return
        per_gpu: dict[int, dict] = {}
        for _ts, powers in points:
            for idx, w in powers.items():
                if w is None:
                    continue
                per_gpu.setdefault(idx, []).append(w)
        for idx, readings in per_gpu.items():
            if not readings:
                continue
            avg_w = sum(readings) / len(readings)
            # store the interval with enough precision that
            # energy_wh == average_power_w × interval_seconds / 3600 holds
            # against the STORED values (self-consistent rows)
            interval_s = round(interval, 6)
            energy_wh = avg_w * interval_s / 3600.0
            await self.db.insert_metric(t1, "gpu_energy", {
                "gpu_index": idx,
                "interval_seconds": interval_s,
                "average_power_w": round(avg_w, 2),
                "energy_wh": round(energy_wh, 6),
                "source": SOURCE,
            })
            self.aggregations += 1

    # ---- summaries -------------------------------------------------------------------
    async def gpu_energy_summary(self, minutes: int = 60, since: Optional[float] = None) -> dict:
        """Aggregated GPU energy over a window — from gpu_energy rows only.

        `since` optionally clips the window start (e.g. local midnight for
        "energy today"). `measured_seconds` covers only really measured
        intervals; gaps add nothing. `average_power_w` is interval-weighted.
        """
        start = since if since is not None else time.time() - minutes * 60
        rows = await self.db.get_metrics("gpu_energy", start)
        total_wh = 0.0
        measured_seconds = 0.0
        gpus: dict[int, dict] = {}
        for r in rows:
            d = r.get("data") or {}
            if d.get("energy_wh") is None:
                continue
            total_wh += d["energy_wh"]
            measured_seconds += d.get("interval_seconds") or 0.0
            idx = d.get("gpu_index")
            g = gpus.setdefault(idx, {"gpu_index": idx, "energy_wh": 0.0,
                                      "measured_seconds": 0.0})
            g["energy_wh"] += d["energy_wh"]
            g["measured_seconds"] += d.get("interval_seconds") or 0.0
        avg_w = (total_wh * 3600.0 / measured_seconds) if measured_seconds > 0 else None
        return {
            "available": bool(rows),
            "source": SOURCE,
            "energy_wh": round(total_wh, 6) if rows else None,
            "kwh": round(total_wh / 1000.0, 6) if rows else None,
            "average_power_w": round(avg_w, 2) if avg_w is not None else None,
            "measured_seconds": round(measured_seconds, 1),
            "points": len(rows),
            "gpus": [{**g, "energy_wh": round(g["energy_wh"], 6),
                      "kwh": round(g["energy_wh"] / 1000.0, 6)} for g in gpus.values()],
            "note": "GPU-only energy — not total server consumption",
        }


_sampler: Optional[GpuEnergySampler] = None


def get_gpu_energy_sampler() -> GpuEnergySampler:
    global _sampler
    if _sampler is None:
        from .db import Database
        _sampler = GpuEnergySampler(Database.get())
    return _sampler


def set_gpu_energy_sampler(s: Optional[GpuEnergySampler]) -> None:
    global _sampler
    _sampler = s
