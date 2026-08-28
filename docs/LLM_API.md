# LLM API

WebOllama manages **OpenAI-compatible** LLM API endpoints. Two endpoints are
pre-configured for the local server (192.168.80.22):

| Name | Base URL |
|---|---|
| **Ollama** | `http://192.168.80.22:11434/v1` |
| **OmniRouter** | `http://192.168.80.22:20128/v1` |

## Provider architecture

```
LLMProvider            (base class — OpenAI-compatible)
├── OllamaProvider     (customisations for Ollama)
└── OmniRouterProvider (customisations for OmniRouter)
```

New providers (OpenRouter, vLLM, LiteLLM, OpenAI, ...) can be added as
subclasses of `LLMProvider` — no frontend changes needed.

## Endpoint management

- **Default endpoints** are loaded from SQLite (`llm_endpoints` setting) on
  first launch.
- **Add API**: admin-only, fields: ID, Name, Base URL, API Key, Enabled.
- **Edit API**: change name, URL, API key, enabled state.
- **Delete API**: remove an endpoint.
- SSRF protection: the backend only connects to stored `base_url` values; the
  frontend never supplies a URL for backend requests.

## Status checks

Each endpoint is automatically checked every 15 seconds via `GET /v1/models`:

- HTTP status code
- Latency (ms)
- Model count (number of items in `data`)
- Error message if unavailable

Results are cached and included in the WebSocket snapshot.

## API key security

- API keys are stored in SQLite (settings table).
- **Never** returned in full via REST — masked: `sk-****3456`.
- **Never** logged.
- The backend adds `Authorization: Bearer <key>` header.
- The frontend sends an empty/masked placeholder on edit; the backend keeps the existing key.

## Models

`GET /api/llm/{id}/models` returns the full model list from the endpoint.
Extra fields beyond the standard OpenAI schema (`id`, `object`, `created`,
`owned_by`) are preserved and displayed:

- **OmniRouter** adds: `context_length`, `max_input_tokens`, `max_output_tokens`,
  `capabilities`, `permission`, `root`, `parent`

## Real data

- **Ollama**: 36 models, ~50 ms latency
- **OmniRouter**: 153 models, ~35 ms latency