#!/usr/bin/env python3
"""Minimal read-only host-agent serving CPU/GPU stats of THIS machine.

Purpose: WebOllama (on 192.168.80.111) shows a Dashboard PROCESSOR block for
the machine that actually runs Ollama + the NVIDIA GPU (192.168.80.22).
This agent is deployed ON the Ollama host and exposes one read-only endpoint:

    GET /api/processor
    -> {"host": "...", "cpu": {...}, "gpus": [...], "timestamp": "..."}

Safety properties:
- stdlib only (no pip installs on the Ollama host);
- psutil is used when present, otherwise pure /proc/stat + /proc/loadavg;
- GPU via `nvidia-smi --query-gpu=...` (no root needed); absent nvidia-smi
  or no NVIDIA GPU -> "gpus": [] with reason, never an error;
- GET only, binds 127.0.0.1 by default (use --bind to expose to LAN),
  2.5s socket timeout on the caller side; single tiny process, no auth
  data, no secrets, no write access, no Ollama API impact;
- ignores unknown query strings; request size irrelevant (no body read).

Run (no root required):
    python3 scripts/ollama22-host-agent.py            # 127.0.0.1:8081
    python3 scripts/ollama22-host-agent.py --bind 192.168.80.22 --port 8081

systemd (recommended, user-level is fine):
    [Service]
    ExecStart=/usr/bin/python3 /opt/ollama22-host-agent.py --bind 0.0.0.0 --port 8081
    Restart=always
"""
import argparse
import json
import os
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_VERSION = "1.0"

_SMI_QUERY = "index,name,uuid,temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total"


# ----------------------------- CPU -----------------------------
def _proc_stat_busy() -> float:
    """CPU busy% from /proc/stat (no psutil needed)."""
    def read():
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = [int(x) for x in line.split()[1:9]]
                    return parts
        return [0] * 8

    a = read()
    time.sleep(0.15)
    b = read()
    da = sum(a)
    db = sum(b)
    idle = (b[3] + b[4]) - (a[3] + a[4])  # idle + iowait
    if db - da <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * (1.0 - idle / (db - da)))), 1)


def cpu_stats() -> dict:
    try:
        import psutil  # type: ignore

        return {
            "utilization": psutil.cpu_percent(interval=None),
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(logical=True),
            "load": [round(x, 2) for x in os.getloadavg()],
        }
    except Exception:
        pass
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        load = None
    phys = None
    try:
        with open("/proc/cpuinfo") as f:
            phys = {frozenset(l.split(":") for l in blk.splitlines() if "physical id" in l or "core id" in l)
                    for blk in f.read().split("\n\n") if "physical id" in blk}
        phys = len(phys) or None
    except Exception:
        phys = None
    logical = os.cpu_count()
    return {
        "utilization": _proc_stat_busy(),
        "cores_physical": phys,
        "cores_logical": logical,
        "load": load,
    }


# ----------------------------- GPU -----------------------------
def gpu_stats() -> list:
    smi = shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"
    if not os.path.exists(smi):
        return []
    try:
        out = subprocess.run(
            [smi, f"--query-gpu={_SMI_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode != 0:
            return []
    except Exception:
        return []
    # column mapping: 0 index, 1 name, 2 uuid, 3 temperature.gpu,
    #                 4 utilization.gpu, 5 utilization.memory,
    #                 6 memory.used, 7 memory.total (MiB, nounits)
    gpus = []
    for line in out.stdout.strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 8:
            continue
        def num(v):
            try:
                return float(v)
            except ValueError:
                return None
        idx = int(cols[0]) if cols[0].isdigit() else len(gpus)
        mu, mt = num(cols[6]), num(cols[7])
        gpus.append({
            "index": idx,
            "name": cols[1],
            "uuid": cols[2],
            "temperature": num(cols[3]),
            "utilization": num(cols[4]),
            "memory_utilization": num(cols[5]),
            "memory_used": mu * 1024 * 1024 if mu is not None else None,
            "memory_total": mt * 1024 * 1024 if mt is not None else None,
        })
    return gpus


class Handler(BaseHTTPRequestHandler):
    server_version = "ollama22-host-agent/" + AGENT_VERSION
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (stdlib API)
        if self.path.split("?")[0] != "/api/processor":
            self._send(404, {"error": "not found"})
            return
        payload = {
            "host": os.uname().nodename,
            "agent_version": AGENT_VERSION,
            "cpu": cpu_stats(),
            "gpus": gpu_stats(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ts": time.time(),
        }
        self._send(200, payload)

    def do_POST(self):  # noqa: N802 — read-only by design
        self._send(405, {"error": "read-only agent"})

    def log_message(self, fmt, *args):  # quiet default; stderr if verbose
        if os.environ.get("AGENT_VERBOSE"):
            super().log_message(fmt, *args)


def main() -> None:
    ap = argparse.ArgumentParser(description="read-only CPU/GPU stats agent")
    ap.add_argument("--bind", default="127.0.0.1", help="bind address (default: loopback)")
    ap.add_argument("--port", type=int, default=8081)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"host-agent listening on {args.bind}:{args.port} (GET /api/processor)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
