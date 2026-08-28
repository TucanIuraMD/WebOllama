# Contributing to WebOllama

Thank you for considering contributing to WebOllama!

## Development Setup

```bash
git clone <repo> /opt/ollama-web
cd /opt/ollama-web
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio  # for tests
```

## Code Style

- **Python**: follow PEP 8. Type hints are required for all public functions.
- **JavaScript**: ES2021+ without transpilation (vanilla JS). Use `const`/`let`, template literals, arrow functions.
- **CSS**: custom properties for theming (`--bg`, `--accent`, etc.). Dark theme only.
- **Shell scripts**: `set -euo pipefail`, `bash -n` valid.

## Commit Messages

Follow conventional commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `style:`.

## Adding an LLM API Provider

1. Create a subclass of `LLMProvider` in `webui/llm_api.py` (e.g. `OpenRouterProvider`).
2. Register it in `make_provider()`.
3. Add the default endpoint config to `DEFAULT_ENDPOINTS` (or document it for manual addition).
4. The frontend renders providers generically — no frontend changes needed for new providers.

## Adding a GPU Metric

1. Add the field to the NVML collector in `gpu_collector.py` (use `_i`, `_s`, `_struct_attr` or `_mw_to_w`).
2. Add it to the nvidia-smi parser if applicable.
3. Add it to the realtime metric persistence in `realtime.py`.
4. Add it to the frontend (dashboard.js, gpu.js, charts.js).
5. Add tests.

## Running Tests

```bash
python -m pytest tests/ -q          # 83+ tests
node scripts/uitest/smoke.mjs       # jsdom frontend test (optional)
bash -n install.sh start.sh stop.sh
```

## Pull Request Process

1. Ensure tests pass.
2. Update documentation if needed.
3. Verify `bash -n` for shell scripts.
4. Target the `main` branch.