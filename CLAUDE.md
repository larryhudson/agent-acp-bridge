# Claude Code Guidelines

## Project Overview

This is an **ACP Bridge** — a FastAPI service that connects external services (Linear, Slack, GitHub) to AI coding agents via the Agent Communication Protocol. Events from external services trigger agent sessions; progress is streamed back in real-time.

### Architecture

```
Event in → ServiceAdapter → SessionManager → AcpSession (subprocess) → UpdateRouter → ServiceAdapter → Event out
```

**Key layers:**
- `app/core/types.py` — `ServiceAdapter` protocol, `BridgeSessionRequest`, `BridgeUpdate` (the core abstractions)
- `app/core/session_manager.py` — Orchestrates session lifecycle, maps external sessions to ACP sessions, handles persistence
- `app/core/update_router.py` — Debounces raw ACP updates (2s buffer) into `BridgeUpdate` objects
- `app/acp/session.py` — Spawns agent subprocess (`claude-code-acp` or `codex-acp`), manages ACP connection
- `app/acp/client.py` — ACP client that auto-approves all permissions for autonomous operation
- `app/services/*/adapter.py` — Service-specific adapters implementing `ServiceAdapter`

### Adding a new service adapter

1. Create `app/services/<name>/` with `adapter.py`, `api_client.py`, `models.py`
2. Implement `ServiceAdapter` protocol (see `app/core/types.py`)
3. Register it in `app/main.py` `_create_adapters()`
4. Add config vars to `app/config.py` and `.env.example`

No changes needed to the bridge core or ACP layer.

## Local Development

The bridge runs in Docker (it needs Node.js for the ACP agent binaries):

```bash
docker compose up -d --build     # Build and start
docker compose logs -f bridge    # Tail logs
docker compose down              # Stop
```

Config is loaded from `.env` via Pydantic Settings (see `app/config.py`). Project repos go in `./data/projects/` — this is mounted into the container at `/data/projects/`.

## Environment Setup

### Pre-commit hooks
- **NEVER skip pre-commit hooks** with `--no-verify`
- If pre-commit fails, fix the environment instead of bypassing it
- Ensure `pre-commit` is in the dev dependencies in `pyproject.toml`
- Run `uv sync` to install dev dependencies including pre-commit
- Use `uv run pre-commit` or activate the venv to run pre-commit

### Running commands with uv
- Use `uv run <command>` to run Python tools (e.g., `uv run ruff check .`)
- Or prefix commands with the venv Python: `.venv/bin/python`
- The PATH may not include uv by default - use `$HOME/.local/bin/uv` if needed

## Package Management

- Use **uv** for Python package management
- Add dev dependencies to `[dependency-groups] dev` in pyproject.toml

## LSP Integration

Real-time diagnostics from language servers are provided via an LSP bridge daemon that runs during Claude Code sessions.

### How it works

1. **SessionStart hook** (`.claude/hooks/session-start.sh`) starts the bridge daemon
2. **LSP bridge** (`.claude/hooks/lsp-bridge.mjs`) spawns language servers defined in config, communicates via LSP JSON-RPC, and exposes an HTTP API on a Unix socket
3. **PostToolUse hook** (`.claude/hooks/lsp-diagnostics-check.sh`) queries the bridge after every Write/Edit, surfacing errors and warnings as context

### Configuring language servers

Servers are defined in `.claude/hooks/lsp-servers.yaml`. To add a new language server, add an entry:

```yaml
  myserver:
    command: my-language-server    # binary name or path
    args: ["--stdio"]              # CLI arguments
    search:                        # project-relative paths to try
      - node_modules/.bin/my-language-server
    cwd: subdir                    # working directory (optional)
    root: subdir                   # LSP workspace root (optional)
    env:                           # extra env vars (optional)
      PATH: "${PROJECT_DIR}/node_modules/.bin:${PATH}"
    requires:                      # only start if these exist
      - tsconfig.json
    extensions:                    # file extension -> LSP language ID
      .xyz: mylang
```

The bridge resolves commands by checking `search` paths first, then falling back to `$PATH`. Servers whose `requires` files don't exist are silently skipped.

### Debugging

- Bridge logs: `.claude/hooks/lsp-bridge.log`
- Health check: `curl --unix-socket "$(cat .claude/hooks/lsp-bridge.socket)" http://localhost/health`
- Manual diagnostics: `curl --unix-socket "$(cat .claude/hooks/lsp-bridge.socket)" -X POST -H "Content-Type: application/json" -d '{"file":"/absolute/path"}' http://localhost/diagnostics`
