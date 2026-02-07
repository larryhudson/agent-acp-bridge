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
┌────────────┐                    │    ├── RepoProvider (shared repo)    │
│   GitHub   │ ── webhooks ──>    │    └── UpdateRouter (debounced)      │
│            │ <── REST API ──    │               │                      │
└────────────┘                    │               ▼                      │
                                  │  ACP Layer                           │
                                  │    └── Spawns agent subprocess       │
                                  │        (claude-code-acp / codex-acp) │
                                  │                                      │
                                  │  /data/projects/  (mounted volume)   │
                                  └──────────────────────────────────────┘
```

Each service adapter implements a common `ServiceAdapter` protocol, so the bridge core and ACP layer are shared. Adding a new service means writing a new adapter — no changes to the core.

### Shared repo and branching

All sessions work on a single configured GitHub repository. When a new session starts, the bridge:

1. Clones or fetches the repo to `/data/projects/<owner>/<repo>/`
2. Creates a unique branch: `acp-agent/<slug>-<timestamp>`
3. Installs skill files (service-specific instructions) for the agent
4. Forwards API tokens so the agent can interact with GitHub, Slack, and Linear

Follow-up messages resume the same session on the same branch with full conversation history.

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
| `ANTHROPIC_API_KEY` | Required for `claude-code-acp` |
| `GITHUB_REPO` | Shared repo for all sessions, e.g. `owner/repo` |
| `GITHUB_INSTALLATION_ID` | GitHub App installation ID (for token generation) |
| `GITHUB_APP_ID` | GitHub App ID |
| `GITHUB_PRIVATE_KEY` | GitHub App PEM key (with `\n` for newlines) |
| `GITHUB_WEBHOOK_SECRET` | GitHub webhook signature verification |
| `LINEAR_WEBHOOK_SECRET` | Linear webhook signature verification |
| `LINEAR_ACCESS_TOKEN` | Linear API token |
| `LINEAR_AGENT_ID` | Your Linear agent integration ID |
| `SLACK_APP_TOKEN` | Slack app-level token (`xapp-`) for Socket Mode |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-`) for Web API |
| `SLACK_USER_TOKEN` | Slack user token (`xoxp-`) for search API |

## Running with Docker

```bash
docker compose up -d --build

# View logs
docker compose logs -f bridge

# Rebuild after code changes
docker compose up -d --build

# Stop
docker compose down
```

The container mounts `./data/projects/` for the cloned repository and uses named volumes for session persistence across restarts.

## Skill files

The `skills/` directory contains service-specific instructions (`SKILL.md`) that are installed into the agent's environment before each session. These teach the agent how to use each service's API (Linear GraphQL, Slack Web API, GitHub CLI).

```
skills/
├── linear/SKILL.md    # Linear GraphQL API usage, mentioning users
├── slack/SKILL.md     # Slack Web API usage (bot + user tokens)
└── github/SKILL.md    # GitHub CLI (gh) usage
```

## Project structure

```
app/
├── main.py                    # FastAPI app, lifespan, adapter registration
├── config.py                  # Pydantic Settings from environment
├── core/
│   ├── types.py               # ServiceAdapter protocol, BridgeUpdate, BridgeSessionRequest
│   ├── session_manager.py     # Orchestrates sessions, handles persistence
│   ├── repo_provider.py       # Shared repo management, branching, skill installation
│   └── update_router.py       # Debounces ACP updates into BridgeUpdates
├── acp/
│   ├── session.py             # Spawns and manages agent subprocess
│   └── client.py              # ACP client with auto-approval for autonomous operation
└── services/
    ├── linear/                # Webhook-based adapter for Linear Agents API
    ├── slack/                 # Socket Mode adapter with real-time message editing
    └── github/                # Webhook-based adapter for GitHub
skills/
├── linear/SKILL.md
├── slack/SKILL.md
└── github/SKILL.md
```
