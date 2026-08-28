# Security Policy

## Authentication

- **Password hashing**: PBKDF2-HMAC-SHA256, 260,000 iterations, 16-byte salt.
- **Sessions**: random 48-byte tokens, stored in SQLite, TTL configurable (default 7 days).
- **Default admin**: created on first run; password must be changed immediately.
- **Transmission**: cookies with `httponly`, `samesite=lax`; also supports `Authorization: Bearer <token>` header.

## CSRF Protection

- All state-changing requests (POST, PUT, DELETE) require `X-CSRF-Token` header when using cookie-based sessions.
- The CSRF token is sent as a non-httponly cookie paired with the session cookie.
- Token-based auth (`Authorization` header) is immune to CSRF by design.

## Rate Limiting

- **General API**: 60 requests per minute per IP.
- **Dangerous operations** (pull, push, create, delete, copy, stop, console, settings): 10 requests per minute per IP.
- Rate limiting is active only when `AUTH_ENABLED=true`.

## Command Whitelist (Ollama Console)

- Only whitelisted commands are allowed: `list`, `ps`, `show`, `pull`, `push`, `create`, `cp`, `rm`, `stop`, `version`, `help`.
- Shell metacharacters (`; | & $ \` < > \n \r " ' ( ) { } [ ]`) are rejected.
- Model names are validated against a strict regex.
- No `shell=True` — subprocess is called with array arguments.
- For remote Ollama, commands are translated to API calls (no subprocess).

## API Key Security

- API keys for LLM endpoints are stored in SQLite (settings table).
- **Never** returned in full via REST — masked: `sk-****3456`.
- **Never** logged.
- The backend adds the `Authorization: Bearer <key>` header when making requests.

## SSRF Protection

- The backend only connects to `base_url` values stored in the endpoint configuration (SQLite).
- `check` and `models` requests reference an endpoint by `id` — they never accept an arbitrary URL from the frontend.
- After a request is deleted, the endpoint is removed, and the base URL is no longer accessible.

## SQL Injection

- All SQL queries use parameterized statements (`?` placeholders).

## Audit Log

All security-relevant actions are logged:
- `login`, `logout`, `change_password`
- `pull`, `push`, `create`, `copy`, `delete`, `stop`
- `console` commands
- `add_llm_endpoint`, `update_llm_endpoint`, `delete_llm_endpoint`
- `update_settings`

## Reporting a Vulnerability

If you find a security issue, please open a GitHub issue or contact the maintainers directly. Do not publicly disclose until it has been addressed.