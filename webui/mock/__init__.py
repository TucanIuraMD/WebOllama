"""Mock mode for tests / dev only. Never used in production.

Provides in-memory stand-ins for the Ollama API, GPU and system collectors so
the backend can be exercised without a real GPU or a real Ollama server.
"""
import json
import time

import httpx


# --------------------------------------------------------------------------- #
# Mock Ollama API — a small in-memory registry + httpx MockTransport handlers.
# --------------------------------------------------------------------------- #
class MockOllama:
    def __init__(self, models=None, running=None):
        self.models = models or [
            {
                "name": "qwen3:8b",
                "model": "qwen3:8b",
                "modified_at": "2026-08-01T10:00:00Z",
                "size": 4_700_000_000,
                "digest": "abc123",
                "details": {
                    "format": "gguf",
                    "family": "qwen3",
                    "families": ["qwen3"],
                    "parameter_size": "7.6B",
                    "quantization_level": "Q4_K_M",
                    "parent_model": "",
                    "context_length": 32768,
                    "embedding_length": 4096,
                },
                "capabilities": ["completion", "tools"],
            },
            {
                "name": "llama3.2:1b",
                "model": "llama3.2:1b",
                "modified_at": "2026-08-02T10:00:00Z",
                "size": 1_300_000_000,
                "digest": "def456",
                "details": {
                    "format": "gguf",
                    "family": "llama",
                    "families": ["llama"],
                    "parameter_size": "1.2B",
                    "quantization_level": "Q4_K_M",
                    "parent_model": "",
                },
                "capabilities": ["completion"],
            },
        ]
        self.running = running or [
            {
                "name": "qwen3:8b",
                "model": "qwen3:8b",
                "size": 4_700_000_000,
                "size_vram": 4_600_000_000,
                "digest": "abc123",
                "expires_at": "2026-08-03T10:00:00Z",
                "details": {"family": "qwen3", "parameter_size": "7.6B", "quantization_level": "Q4_K_M"},
            }
        ]

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            body = {}
            if request.content:
                try:
                    body = json.loads(request.content)
                except ValueError:
                    body = {}
            if path == "/api/version":
                return httpx.Response(200, json={"version": "0.32.6"})
            if path == "/api/tags":
                return httpx.Response(200, json={"models": self.models})
            if path == "/api/ps":
                return httpx.Response(200, json={"models": self.running})
            if path == "/api/show":
                model = next((m for m in self.models if m["name"] == body.get("name")), None)
                if not model:
                    return httpx.Response(404, json={"error": "model not found"})
                d = model.get("details", {})
                family = d.get("family", "mock")
                return httpx.Response(200, json={
                    "model": model["name"],
                    "modelfile": "FROM " + model["name"],
                    "parameters": {"temperature": "0.7"},
                    "template": "{{ .Prompt }}",
                    "system": "mock system",
                    "license": "MIT",
                    "details": d,
                    "capabilities": model.get("capabilities", []),
                    "model_info": {
                        "general.architecture": family,
                        f"{family}.context_length": d.get("context_length", 4096),
                    },
                })
            if path == "/api/copy":
                src = body.get("source", "")
                dst = body.get("destination", "")
                src_model = next((m for m in self.models if m["name"] == src), None)
                if not src_model:
                    return httpx.Response(404, json={"error": f"model '{src}' not found"})
                self.models.append(dict(src_model, name=dst, digest="copy" + dst))
                return httpx.Response(200, json={})
            if path == "/api/delete":
                name = body.get("model", "")
                self.models = [m for m in self.models if m["name"] != name]
                self.running = [m for m in self.running if m["name"] != name]
                return httpx.Response(200, json={})
            if path == "/api/generate":
                model = body.get("model", "")
                if not any(m["name"] == model for m in self.models):
                    return httpx.Response(404, json={"error": "model not found"})
                if body.get("keep_alive") == 0:
                    # official unload mechanism: keep_alive=0 drops from VRAM
                    self.running = [m for m in self.running if m["name"] != model]
                    return httpx.Response(200, json={"model": model, "done": True, "response": ""})
                if not body.get("prompt") and not body.get("raw"):
                    # empty-prompt generate = preload/load into VRAM
                    if not any(m["name"] == model for m in self.running):
                        src = next(m for m in self.models if m["name"] == model)
                        self.running.append(dict(src, size_vram=src["size"]))
                    return httpx.Response(200, json={
                        "model": model, "done": True, "response": "",
                    })
                return httpx.Response(200, json={"done": True, "response": "mock"})
            if path == "/api/chat":
                if not any(m["name"] == body.get("model") for m in self.models):
                    return httpx.Response(404, json={"error": "model not found"})
                if body.get("stream"):
                    # NDJSON: incremental token chunks, then a final done line
                    # carrying the timing/eval counters (mirrors real Ollama).
                    chunks = ["mock ", "chat ", "reply"]
                    lines = [
                        json.dumps({"model": body.get("model", ""), "done": False,
                                    "message": {"role": "assistant", "content": c}})
                        for c in chunks
                    ]
                    lines.append(json.dumps({
                        "model": body.get("model", ""),
                        "done": True,
                        "message": {"role": "assistant", "content": ""},
                        "total_duration": 1_234_567,
                        "prompt_eval_count": 5,
                        "eval_count": 3,
                    }))
                    return httpx.Response(200, text="\n".join(lines) + "\n")
                return httpx.Response(200, json={
                    "model": body.get("model", ""),
                    "done": True,
                    "message": {"role": "assistant", "content": "mock chat reply"},
                    "total_duration": 1_234_567,
                    "prompt_eval_count": 5,
                    "eval_count": 3,
                })
            if path in ("/api/pull", "/api/push", "/api/create"):
                name = body.get("name", "")
                # stream a few progress lines then success
                lines = [
                    {"status": "pulling manifest"},
                    {"status": "downloading", "completed": 0, "total": 100},
                    {"status": "downloading", "completed": 100, "total": 100},
                    {"status": "success"},
                ]
                if path == "/api/create":
                    if not body.get("from") and not body.get("files"):
                        return httpx.Response(400, json={"error": "neither 'from' or 'files' was specified"})
                    self.models.append({"name": name, "size": 1, "digest": "created", "details": {}})
                return httpx.Response(200, text="\n".join(json.dumps(l) for l in lines) + "\n")
            return httpx.Response(404, json={"error": "not found"})

        return httpx.MockTransport(handler)


def make_mock_ollama_client(models=None, running=None) -> "OllamaClient":
    """Return an OllamaClient wired to a mock transport (no network)."""
    from ..ollama_client import OllamaClient

    mock = MockOllama(models, running)
    client = OllamaClient(base_url="http://mock")
    client._client = httpx.AsyncClient(transport=mock.transport(), base_url="http://mock")
    return client


# --------------------------------------------------------------------------- #
# Mock GPU collector — returns a Tesla V100-like snapshot. Tests/dev only.
# --------------------------------------------------------------------------- #
class MockGPUCollector:
    def __init__(self):
        self.data = {
            "available": True,
            "source": "mock",
            "driver_version": "535.183.01",
            "cuda_version": "12.2",
            "gpus": [
                {
                    "index": 0,
                    "name": "Tesla V100-SXM2-16GB",
                    "driver_version": "535.183.01",
                    "cuda_version": "12.2",
                    "temperature": 61,
                    "utilization": 78,
                    "memory_utilization": 46,
                    "vram_total": 16_160_000_000,
                    "vram_used": 12_400_000_000,
                    "vram_free": 3_760_000_000,
                    "power_draw": 185.0,
                    "power_limit": 250.0,
                    "fan": 30,
                    "clocks": 1530,
                    "mem_clock": 877,
                    "pstate": "P0",
                    "pcie_gen": 3,
                    "pcie_width": 16,
                    "pcie_gen_max": 3,
                    "pcie_width_max": 16,
                    "pci_bus": "0000:3B:00.0",
                    "compute_mode": "Default",
                    "persistence_mode": "Enabled",
                    "display_active": "Disabled",
                }
            ],
            "processes": [
                {"pid": 1234, "name": "ollama", "used_memory": 12_100_000_000, "gpu_index": 0},
                {"pid": 5678, "name": "python", "used_memory": 200_000_000, "gpu_index": 0},
            ],
            "ts": time.time(),
        }

    async def sample(self) -> dict:
        return self.data

    async def gpu_processes(self) -> dict:
        return {"available": True, "processes": self.data["processes"]}

    async def ollama_vram(self, running: list[dict]) -> dict:
        total = sum(m.get("size_vram", 0) for m in running)
        return {"total_vram": total, "per_model": {m.get("name", ""): m.get("size_vram", 0) for m in running}}


def install_mocks():
    """Install mock collectors into the singleton getters. Tests only."""
    import webui.gpu_collector as gc
    import webui.ollama_client as oc

    client = make_mock_ollama_client()
    oc._client = client

    mock_gpu = MockGPUCollector()
    gc._collector = mock_gpu

    # system collector: psutil works fine on any host; nothing to mock
    return client
