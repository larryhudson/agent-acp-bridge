# ACP Bridge

A service-agnostic bridge that connects external services (Linear, Slack, GitHub) to AI coding agents via the [Agent Communication Protocol (ACP)](https://agentclientprotocol.com/). When an event comes in — a Linear ticket assignment, a Slack @mention, a GitHub issue comment — the bridge spawns an ACP-compatible agent (Claude Code or Codex), streams progress updates back to the originating service, and persists session state so conversations can be resumed across container restarts.

## How it works

```
External Services                  Docker Container
                                  ┌──────────────────────────────────────┐
┌────────────┐                    │  FastAPI Server                      │
│   Linear   │ ── webhooks ──>    │                                      │
│            │ <── GraphQL ──     │  Service Adapters (pluggable)        │
└────────────┘                    │    ├── LinearAdapter (webhooks)      │
                                  │    ├── SlackAdapter (Socket Mode)    │
┌────────────┐                    │    └── GitHubAdapter (webhooks)      │
│   Slack    │ ── Socket Mode ──> │               │                      │
│            │ <── Web API ──     │               ▼                      │
└────────────┘                    │  Bridge Core                         │
                                  │    ├── SessionManager                │
┌────────────┐                    │    └── UpdateRouter (debounced)      │
│   GitHub   │ ── webhooks ──>    │               │                      │
│            │ <── GraphQL ──     │               ▼                      │
└────────────┘                    │  ACP Layer                           │
                                  │    └── Spawns agent subprocess       │
                                  │        (claude-code-acp / codex-acp) │
                                  │                                      │
                                  │  /data/projects/  (mounted volume)   │
                                  └──────────────────────────────────────┘
```

Each service adapter implements a common `ServiceAdapter` protocol, so the bridge core and ACP layer are shared. Adding a new service means writing a new adapter — no changes to the core.

## Setup

```bash
cp .env.example .env
# Fill in your API keys and tokens (see below)
```

### Environment variables

| Variable | Description |
|---|---|
| `ACP_AGENT_COMMAND` | Agent binary to spawn: `claude-code-acp` or `codex-acp` |
| `ENABLED_SERVICES` | Comma-separated list: `linear`, `slack`, `github` |
| `PROJECT_MAPPINGS_JSON` | Maps service contexts to project directories (see below) |
| `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` | Required for `claude-code-acp` |
| `LINEAR_WEBHOOK_SECRET` | Linear webhook signature verification |
| `LINEAR_ACCESS_TOKEN` | Linear API token |
| `LINEAR_AGENT_ID` | Your Linear agent integration ID |
| `SLACK_APP_TOKEN` | Slack app-level token (`xapp-`) for Socket Mode |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-`) for Web API |

### Project mappings

`PROJECT_MAPPINGS_JSON` tells the bridge which codebase to use for each service context:

```json
{
  "linear:TEAM_ID": "/data/projects/my-repo",
  "slack:CHANNEL_ID": "/data/projects/other-repo"
}
```

## Running with Docker

```bash
docker compose up -d

# View logs
docker compose logs -f bridge

# Stop
docker compose down
```

The container mounts `./data/projects/` for your code repositories and uses named volumes for session persistence across restarts.

## Local development

```bash
# Install dependencies
uv sync

# Start the server
make dev

# View logs
make dev-logs
```

## Project structure

```
app/
├── main.py                    # FastAPI app, lifespan, adapter registration
├── config.py                  # Pydantic Settings from environment
├── core/
│   ├── types.py               # ServiceAdapter protocol, BridgeUpdate, BridgeSessionRequest
│   ├── session_manager.py     # Orchestrates sessions, handles persistence
│   └── update_router.py       # Debounces ACP updates into BridgeUpdates
├── acp/
│   ├── session.py             # Spawns and manages agent subprocess
│   └── client.py              # ACP client with auto-approval for autonomous operation
└── services/
    ├── linear/                # Webhook-based adapter for Linear Agents API
    ├── slack/                 # Socket Mode adapter with real-time message editing
    └── github/                # Webhook-based adapter for GitHub
```
