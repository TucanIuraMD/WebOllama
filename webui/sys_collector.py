"""System monitoring via psutil — CPU, RAM, disk, network, processes."""
import asyncio
import logging
import os
import time
from typing import Optional

import psutil

logger = logging.getLogger(__name__)


class SystemCollector:
    def __init__(self, sample_interval: float = 0.4) -> None:
        self.sample_interval = sample_interval
        self._lock = asyncio.Lock()
        self._last: dict = {}
        self._last_net: Optional[tuple] = None  # (ts, dict[iface] -> (bytes_sent, bytes_recv))
        self._boot_time = psutil.boot_time()

    async def sample(self) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._sample_sync)

    def _sample_sync(self) -> dict:
        # CPU
        cpu_percent = psutil.cpu_percent(interval=None)  # non-blocking: needs history
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
        cpu_freq = None
        try:
            f = psutil.cpu_freq()
            if f:
                cpu_freq = {"current": f.current, "min": f.min, "max": f.max}
        except Exception:
            pass
        cpu = {
            "percent": cpu_percent,
            "load_1": load[0] if load else 0,
            "load_5": load[1] if load else 0,
            "load_15": load[2] if load else 0,
            "cores": psutil.cpu_count(logical=False) or 0,
            "threads": psutil.cpu_count(logical=True) or 0,
            "frequency": cpu_freq,
            "per_core": psutil.cpu_percent(interval=None, percpu=True),
        }

        # RAM
        vm = psutil.virtual_memory()
        ram = {
            "total": vm.total,
            "used": vm.used,
            "free": vm.free,
            "cached": getattr(vm, "cached", 0),
            "buffers": getattr(vm, "buffers", 0),
            "available": vm.available,
            "percent": vm.percent,
        }
        try:
            sw = psutil.swap_memory()
            swap = {"total": sw.total, "used": sw.used, "free": sw.free, "percent": sw.percent}
        except Exception:
            swap = {"total": 0, "used": 0, "free": 0, "percent": 0}

        # Disk
        disks = []
        seen = set()
        for part in psutil.disk_partitions(all=False):
            if part.device in seen:
                continue
            seen.add(part.device)
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            disks.append({
                "device": part.device,
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            })
        disk_total = sum(d["total"] for d in disks)
        disk_used = sum(d["used"] for d in disks)
        disk = {
            "total": disk_total,
            "used": disk_used,
            "free": disk_total - disk_used,
            "percent": (disk_used / disk_total * 100) if disk_total else 0,
            "partitions": disks,
        }

        # Network (delta based)
        net_io = psutil.net_io_counters(pernic=True)
        now = time.time()
        interfaces = {}
        prev = self._last_net
        prev_ts = self._last_net_ts if hasattr(self, "_last_net_ts") else None
        for name, counters in net_io.items():
            if name.startswith("lo"):
                continue
            rx, tx = counters.bytes_recv, counters.bytes_sent
            rx_rate = tx_rate = 0.0
            if prev and prev_ts and name in prev:
                dt = now - prev_ts
                if dt > 0:
                    rx_rate = (rx - prev[name][1]) / dt
                    tx_rate = (tx - prev[name][0]) / dt
            interfaces[name] = {
                "rx": rx,
                "tx": tx,
                "rx_rate": max(0.0, rx_rate),
                "tx_rate": max(0.0, tx_rate),
            }
        self._last_net = {n: (c["tx"], c["rx"]) for n, c in interfaces.items()}
        self._last_net_ts = now

        total_rx = sum(i["rx_rate"] for i in interfaces.values())
        total_tx = sum(i["tx_rate"] for i in interfaces.values())
        network = {
            "rx_rate": total_rx,
            "tx_rate": total_tx,
            "rx_total": sum(i["rx"] for i in interfaces.values()),
            "tx_total": sum(i["tx"] for i in interfaces.values()),
            "interfaces": interfaces,
        }

        # Processes (top CPU / RAM)
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info"]):
            try:
                info = p.info
                if info["memory_info"]:
                    rss = info["memory_info"].rss
                else:
                    rss = 0
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"] or "",
                    "cpu": info["cpu_percent"] or 0.0,
                    "memory": info["memory_percent"] or 0.0,
                    "rss": rss,
                })
            except Exception:
                continue
        procs.sort(key=lambda x: (x["cpu"], x["rss"]), reverse=True)
        processes = {"top_cpu": procs[:10], "top_memory": sorted(procs, key=lambda x: x["rss"], reverse=True)[:10]}

        return {
            "hostname": os.uname().nodename,
            "os": f"{os.uname().sysname} {os.uname().release}",
            "uptime": int(time.time() - self._boot_time),
            "boot_time": self._boot_time,
            "cpu": cpu,
            "ram": ram,
            "swap": swap,
            "disk": disk,
            "network": network,
            "processes": processes,
            "ts": time.time(),
        }

    async def ollama_process(self, pid: int | None = None) -> dict | None:
        """Find the ollama process info (by pid hint or by name)."""
        if pid:
            try:
                p = psutil.Process(pid)
                return await self._proc_info(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if "ollama" in (p.info["name"] or "").lower() or (
                    p.info["cmdline"] and "ollama" in " ".join(p.info["cmdline"]).lower()
                ):
                    return await self._proc_info(p)
            except Exception:
                continue
        return None

    async def _proc_info(self, p) -> dict:
        def _g():
            try:
                with p.oneshot():
                    return {
                        "pid": p.pid,
                        "name": p.name(),
                        "cpu": p.cpu_percent(interval=None),
                        "memory": p.memory_percent(),
                        "rss": p.memory_info().rss,
                        "vms": p.memory_info().vms,
                        "cmdline": p.cmdline(),
                        "create_time": p.create_time(),
                        "status": p.status(),
                    }
            except Exception as e:
                return {"pid": p.pid, "error": str(e)}
        return await asyncio.to_thread(_g)


_collector: Optional[SystemCollector] = None


def get_system_collector() -> SystemCollector:
    global _collector
    if _collector is None:
        _collector = SystemCollector()
    return _collector
