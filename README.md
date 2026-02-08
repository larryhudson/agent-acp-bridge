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

### Repository management and worktrees

Sessions work on GitHub repositories with concurrent isolation via git worktrees. When a new session starts, the bridge:

1. Clones or fetches the repo to `/data/projects/<owner>/<repo>/` (bare repository)
2. Creates a unique branch: `acp-agent/<slug>-<timestamp>`
3. Creates an isolated worktree at `/data/worktrees/<owner>/<repo>/<slug>-<timestamp>/` for the session
4. Installs skill files (service-specific instructions) for the agent
5. Forwards API tokens so the agent can interact with GitHub, Slack, and Linear

Each session operates in its own worktree, enabling multiple agents to work concurrently on different branches without conflicts. Follow-up messages resume the same session on the same branch with full conversation history.

## Setup

```bash
cp .env.example .env
# Fill in your API keys and tokens (see below)
```

### Environment variables

#### Core configuration

| Variable | Description |
|---|---|
| `ACP_AGENT_COMMAND` | Agent binary for single-agent mode: `claude-code-acp` or `codex-acp` |
| `AGENTS_JSON` | Multi-agent config (overrides `ACP_AGENT_COMMAND`), e.g. `{"claude": {"command": "claude-code-acp", "default": true}}` |
| `ENABLED_SERVICES` | Comma-separated list: `linear`, `slack`, `github` |
| `ANTHROPIC_API_KEY` | Required for `claude-code-acp` |
| `OPENAI_API_KEY` | Required for `codex-acp` |
| `BRIDGE_BASE_URL` | Optional: base URL for session viewer links in messages |

#### GitHub

| Variable | Description |
|---|---|
| `GITHUB_REPO` | Default repo for all sessions, e.g. `owner/repo` |
| `GITHUB_INSTALLATION_ID` | GitHub App installation ID (for non-webhook sessions) |
| `GITHUB_APP_ID` | GitHub App ID (default agent or single-agent mode) |
| `GITHUB_PRIVATE_KEY` | GitHub App PEM key (with `\n` for newlines) |
| `GITHUB_WEBHOOK_SECRET` | GitHub webhook signature verification |
| `GITHUB_BOT_LOGIN` | Optional: bot username if auto-detect fails |
| `GITHUB_APP_ID__<AGENT>` | Per-agent GitHub App ID (multi-agent mode) |
| `GITHUB_PRIVATE_KEY__<AGENT>` | Per-agent GitHub private key |
| `GITHUB_WEBHOOK_SECRET__<AGENT>` | Per-agent webhook secret |

#### Linear

| Variable | Description |
|---|---|
| `LINEAR_WEBHOOK_SECRET` | Linear webhook signature verification |
| `LINEAR_ACCESS_TOKEN` | Linear API token |
| `LINEAR_AGENT_ID` | Your Linear agent integration ID |

#### Slack

| Variable | Description |
|---|---|
| `SLACK_APP_TOKEN` | Slack app-level token (`xapp-`) for Socket Mode |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-`) for Web API |
| `SLACK_USER_TOKEN` | Slack user token (`xoxp-`) for search API |
| `SLACK_BOT_TOKEN__<AGENT>` | Per-agent Slack bot token (multi-agent mode) |
| `SLACK_APP_TOKEN__<AGENT>` | Per-agent Slack app token |
| `SLACK_CHANNEL_REPOS` | JSON mapping of channel IDs to repos, e.g. `{"C123":"owner/repo"}` |
| `SLACK_CHANNEL_PROMPTS` | JSON mapping of channel IDs to context prompts |

### Multi-agent setup

The bridge supports running multiple agents simultaneously with different credentials and configurations. To enable multi-agent mode, set the `AGENTS_JSON` variable:

```bash
AGENTS_JSON='{"claude": {"command": "claude-code-acp", "default": true}, "codex": {"command": "codex-acp"}}'
```

Each agent can have its own service credentials using the `__AGENTNAME` suffix pattern:

```bash
# Default agent GitHub credentials
GITHUB_APP_ID=12345
GITHUB_PRIVATE_KEY="-----BEGIN RSA..."

# Codex agent GitHub credentials
GITHUB_APP_ID__CODEX=67890
GITHUB_PRIVATE_KEY__CODEX="-----BEGIN RSA..."

# Slack credentials per agent
SLACK_BOT_TOKEN=xoxb-default-token
SLACK_BOT_TOKEN__CODEX=xoxb-codex-token
```

When `AGENTS_JSON` is set, users can interact with different agents in the same channels/issues by mentioning them (e.g., `@claude-bot` vs `@codex-bot` in Slack). For Slack and GitHub sessions, session IDs include the agent name to maintain separate conversation contexts.

### Slack channel-specific configuration

Different Slack channels can be configured with custom behavior:

**Per-channel repositories** — Route different channels to different repos:
```bash
SLACK_CHANNEL_REPOS='{"C123ABC":"myorg/frontend","C456DEF":"myorg/backend"}'
```

Channels without a mapping fall back to the default `GITHUB_REPO`.

**Per-channel context prompts** — Add channel-specific instructions:
```bash
SLACK_CHANNEL_PROMPTS='{"C123ABC":"You are debugging production alerts. Be concise.","C456DEF":"Explain things simply for non-technical users."}'
```

Context prompts are prepended to the user's message on new sessions and affect agent behavior for that conversation.

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

The container mounts:
- `./data/projects/` — Bare repositories (one per GitHub repo)
- `./data/worktrees/` — Active session worktrees (isolated working directories)
- Named volumes for session state persistence across restarts

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
