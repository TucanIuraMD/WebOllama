# Ollama

WebOllama manages a local Ollama server through the native HTTP API
(`http://127.0.0.1:11434`) and optionally through the local `ollama` CLI
binary for the Console.

## Operations

| Operation | Ollama API | WebOllama UI |
|---|---|---|
| List models | `GET /api/tags` | Models page |
| Show details | `POST /api/show` | Show modal |
| Running models | `GET /api/ps` | Running page + Dashboard |
| Pull | `POST /api/pull` (streaming) | Models → Pull (as Job) |
| Push | `POST /api/push` (streaming) | Models → Push (as Job) |
| Create | `POST /api/create` (streaming) | Models → Create (visual / raw Modelfile) |
| Copy | `POST /api/copy` | Models → Copy |
| Delete | `DELETE /api/delete` | Models → Delete (with confirmation) |
| Stop (unload) | `POST /api/generate` with `keep_alive=0` | Running → Unload |
| Version | `GET /api/version` | Console → `ollama version` |
| Chat | `POST /api/chat` | Terminal → via API |
| Embed | `POST /api/embed` | (future) |

## Jobs

Operations that take time (pull, push, create, delete, copy, stop) run as
**Jobs** — async tasks with progress tracking, WebSocket updates, SQLite
persistence, and cancellation support.

## Modelfile

The Create page supports both a **visual editor** (fields: FROM, SYSTEM,
TEMPLATE, PARAMETER, LICENSE) and a **raw Modelfile** text editor.

The backend parses the Modelfile into the field-based schema that the Ollama
version on this server expects (some builds require `from`/`system`/`parameters`
fields instead of the modelfile string).

## Console

The Ollama Console page accepts whitelisted `ollama` commands:

```
ollama list
ollama ps
ollama show <model>
ollama pull <model>
ollama push <model>
ollama create <model> [-f <modelfile>]
ollama cp <source> <destination>
ollama rm <model>
ollama stop <model>
ollama version
ollama help
```

- If the `ollama` binary is on `$PATH` and `OLLAMA_URL` points at localhost,
  commands are executed via subprocess (array args, no shell).
- Otherwise, commands are translated to the HTTP API.
- Shell metacharacters are rejected; model names are validated against a strict
  regex.

## Offline behaviour

When Ollama is not reachable, the UI shows a clear "OLLAMA OFFLINE" indicator
and model operations return 502 with the error message. No data is fabricated.