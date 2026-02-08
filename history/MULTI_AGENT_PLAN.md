# Multi-Agent Support Plan

## Overview

Today, the bridge runs a single agent binary (e.g. `claude-code-acp`) for all sessions. This plan adds support for **multiple agents** — each backed by its own app installation (separate Slack bot, separate GitHub App) so users can @mention specific agents by name.

The motivating use case: have Claude work on a task, then ask Codex to review Claude's output — all within the same Slack thread or GitHub issue.

## Design Principles

1. **One installation per agent** — separate Slack apps, separate GitHub Apps, separate Linear agent installations. Clearest UX: users interact with distinct bot identities.
2. **Adapters become parameterized** — instead of one `SlackAdapter` reading from global config, each adapter instance receives its agent name, credentials, and route path.
3. **Session IDs include agent name** — so multiple agents can operate in the same thread/issue without colliding.
4. **Core stays untouched** — `AcpSession`, `UpdateRouter`, `BridgeAcpClient` don't change. `SessionManager` needs only a small change to read the agent command from the request instead of global config.

## Current State

```
Config:  ACP_AGENT_COMMAND = "claude-code-acp"   (single global setting)
Session: _active_sessions["slack:C123:thread_ts"]  (one session per thread)
Adapter: SlackAdapter reads SLACK_BOT_TOKEN from settings
         GitHubAdapter reads GITHUB_WEBHOOK_SECRET from settings
```

## Target State

```
Config:  AGENTS = {"claude": {"command": "claude-code-acp", "default": true},
                   "codex":  {"command": "codex-acp"}}

Session: _active_sessions["slack:C123:thread_ts:claude"]   (agent name in key)
         _active_sessions["slack:C123:thread_ts:codex"]    (separate session, same thread)

Adapter: SlackAdapter("claude", bot_token=..., app_token=...)    → Socket Mode connection 1
         SlackAdapter("codex",  bot_token=..., app_token=...)    → Socket Mode connection 2
         GitHubAdapter("claude", webhook_secret=...) → /webhooks/github
         GitHubAdapter("codex",  webhook_secret=...) → /webhooks/github/codex
         LinearAdapter("claude", webhook_secret=..., access_token=...) → /webhooks/linear
         LinearAdapter("codex",  webhook_secret=..., access_token=...) → /webhooks/linear/codex
```

## Conversation Flow Example

### Slack

```
User:      @claude fix the login bug
Claude:    [works on it, posts updates in thread]
Claude:    Done — PR #42 is up

User:      @codex review PR #42 and check for edge cases
Codex:     [new session, same thread, independent context]
Codex:     Found two issues: ...

User:      thanks, can you also check the tests?
           (plain message in thread — no @mention)
```

The last message is a thread follow-up with no @mention. Since both agents have sessions in this thread, the bridge needs a disambiguation rule. **Rule: untagged follow-ups go to the most recently active agent in that thread.** This is tracked per-adapter — each adapter instance only sees follow-ups for its own bot's threads.

Actually, this resolves naturally with the one-installation-per-agent model: Slack delivers `app_mention` events to the specific bot that was @mentioned, and `message` events to bots subscribed to the channel. Each `SlackAdapter` instance only tracks threads where *its* bot was mentioned (via `_active_threads`). So an untagged follow-up in a thread where both bots were mentioned will be delivered to *both* adapter instances, and each will treat it as a follow-up to its own session. This could be intentional ("both of you, what do you think?") or confusing.

**Recommendation:** For now, require @mentions for follow-ups when multiple agents are active in the same thread. The adapter can check: "is there another agent with a session in this thread?" and if so, ignore untagged messages. This is a refinement for later — the initial implementation can start with the simpler "every adapter sees all thread messages" behavior and we can add the guard after seeing how it works in practice.

### GitHub

```
User:      @claude-bot fix the login bug
Claude:    [works, posts progress comment, opens PR]

User:      @codex-bot review this PR and check for security issues
Codex:     [new session, same issue, reviews the code]
```

Each GitHub App has its own bot login, webhook URL, and webhook secret. The adapter instance knows its own bot login and only responds to @mentions of that login.

## Changes by File

### 1. `app/config.py` — Agent registry

Add an `AgentConfig` model and a dict of agents to `Settings`.

```python
from pydantic import BaseModel

class AgentConfig(BaseModel):
    """Configuration for a single ACP agent."""
    command: str                    # e.g. "claude-code-acp", "codex-acp"
    default: bool = False           # Is this the default agent?

class Settings(BaseSettings):
    # Replace single acp_agent_command with agent registry
    # acp_agent_command: str = "claude-code-acp"   # REMOVE
    agents_json: str = ""  # JSON string: {"claude": {"command": "claude-code-acp", "default": true}}

    # Per-agent service credentials (keyed by agent name)
    # Default agent uses the existing env vars (backward compatible)
    # Additional agents use VARNAME__AGENTNAME convention
    # e.g. SLACK_BOT_TOKEN__CODEX, GITHUB_WEBHOOK_SECRET__CODEX

    @property
    def agents(self) -> dict[str, AgentConfig]:
        """Parse agent registry from JSON config."""
        if not self.agents_json:
            # Backward compatible: single agent from legacy config
            return {"default": AgentConfig(command=self.acp_agent_command, default=True)}
        import json
        raw = json.loads(self.agents_json)
        return {name: AgentConfig(**cfg) for name, cfg in raw.items()}

    @property
    def default_agent_name(self) -> str:
        for name, cfg in self.agents.items():
            if cfg.default:
                return name
        # Fallback to first agent
        return next(iter(self.agents))

    def get_agent_config(self, agent_name: str) -> AgentConfig:
        return self.agents[agent_name]
```

Per-agent credentials are handled by a helper that checks for agent-suffixed env vars:

```python
    def get_service_credential(self, var_name: str, agent_name: str) -> str:
        """Get a service credential, checking agent-specific override first.

        For the default agent, uses the base env var (e.g. SLACK_BOT_TOKEN).
        For other agents, checks SLACK_BOT_TOKEN__CODEX first, falls back to base.
        """
        if agent_name == self.default_agent_name:
            return getattr(self, var_name.lower(), "")

        # Check for agent-specific override
        suffixed = f"{var_name}__{agent_name}".upper()
        import os
        value = os.environ.get(suffixed, "")
        if value:
            return value

        # Fall back to base
        return getattr(self, var_name.lower(), "")
```

### 2. `app/core/types.py` — Add agent_name to BridgeSessionRequest

```python
@dataclass
class BridgeSessionRequest:
    external_session_id: str
    service_name: str
    prompt: str
    agent_name: str = ""              # NEW — which agent to use
    descriptive_name: str = ""
    is_followup: bool = False
    service_metadata: dict[str, Any] | None = None
```

### 3. `app/core/session_manager.py` — Use agent_name for command lookup

Two changes:

**a) `handle_new_session`** — look up agent command from config instead of `settings.acp_agent_command`:

```python
# Before:
acp_session = AcpSession(
    command=settings.acp_agent_command,
    ...
)

# After:
agent_config = settings.get_agent_config(request.agent_name)
acp_session = AcpSession(
    command=agent_config.command,
    ...
)
```

**b) `handle_followup`** — same change, but needs to know which agent. Store `agent_name` in `ActiveSession`:

```python
@dataclass
class ActiveSession:
    external_session_id: str
    service_name: str
    agent_name: str = ""    # NEW
    adapter: ServiceAdapter
    acp_session: AcpSession | None
    update_router: UpdateRouter | None
    acp_session_id: str
    cwd: str
    branch_name: str = ""
    service_metadata: dict[str, Any] | None = None
```

Then in `handle_followup`:

```python
agent_config = settings.get_agent_config(active.agent_name)
acp_session = AcpSession(
    command=agent_config.command,
    ...
)
```

**c) Persistence** — add `agent_name` to `to_dict()` / `from_dict()` so it survives restarts.

### 4. `app/services/slack/adapter.py` — Parameterized constructor

The adapter receives its agent name and credentials at construction time instead of reading from global config.

```python
class SlackAdapter:
    def __init__(
        self,
        session_manager: Any,
        agent_name: str,
        bot_token: str,
        app_token: str,
    ) -> None:
        self._session_manager = session_manager
        self._agent_name = agent_name
        self.service_name = f"slack:{agent_name}"  # Unique per agent
        self._api = SlackApiClient(bot_token)
        self._socket_client = SlackSocketClient(
            app_token=app_token,
            bot_token=bot_token,
            on_event=self._handle_event,
        )
        ...
```

**Session ID change** — include agent name:

```python
# Before:
session_id = f"slack:{channel}:{thread_ts}"

# After:
session_id = f"slack:{channel}:{thread_ts}:{self._agent_name}"
```

**BridgeSessionRequest** — set agent_name:

```python
request = BridgeSessionRequest(
    external_session_id=session_id,
    service_name="slack",           # Keep "slack" for repo/project mapping
    agent_name=self._agent_name,    # NEW
    prompt=prompt,
    ...
)
```

**service_name for session manager** — The `service_name` field is used in two places:
1. `restore_sessions_for_adapter` — matches adapter to its sessions. Must be unique per adapter instance, so use `f"slack:{agent_name}"`.
2. `get_cwd_for_key` / project mappings — uses service name for CWD lookup. Keep using `"slack"` for the lookup key so project mappings work the same regardless of which agent.

So we need two properties: `service_name` (unique, for session matching) and `service_type` (for project mappings). Or simpler: keep `service_name` as `"slack"` on the `BridgeSessionRequest` and add the agent qualification only on the adapter's `service_name` property.

**Decision:** The adapter's `service_name` attribute becomes `f"slack:{agent_name}"` for unique session tracking. The `BridgeSessionRequest.service_name` stays `"slack"` for project mapping. `restore_sessions_for_adapter` matches on the adapter's `service_name`.

### 5. `app/services/github/adapter.py` — Parameterized constructor + routes

```python
class GitHubAdapter:
    def __init__(
        self,
        session_manager: Any,
        agent_name: str,
        webhook_secret: str,
        route_path: str = "/webhooks/github",
        auth: GitHubAuth | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._agent_name = agent_name
        self.service_name = f"github:{agent_name}"
        self._webhook_secret = webhook_secret
        self._route_path = route_path
        ...
```

**Route registration** — use the parameterized path:

```python
def register_routes(self, app: FastAPI) -> None:
    @app.post(self._route_path)
    async def handle_github_webhook(request: Request) -> Response:
        ...
        # Use self._webhook_secret instead of settings.github_webhook_secret
        if self._webhook_secret and not verify_signature(
            raw_body, signature, self._webhook_secret
        ):
            ...
```

**Session ID change:**

```python
# Before:
session_id = f"github:{repo.full_name}:{issue_number}"

# After:
session_id = f"github:{repo.full_name}:{issue_number}:{self._agent_name}"
```

**Bot login detection:** Each GitHub App installation has its own bot login (e.g. `claude-bridge[bot]`, `codex-bridge[bot]`). The existing `_extract_mention` logic works as-is — each adapter instance only responds to mentions of its own bot.

**Separate GitHub Apps vs separate installations of the same app:** Separate apps is cleaner — each has its own credentials, its own bot user, its own webhook URL. Separate installations of the *same* app would mean the same bot user responds to both, which defeats the purpose. So: **one GitHub App per agent**.

### 5.5. `app/services/linear/adapter.py` — Parameterized constructor + routes

Same treatment as GitHub. Each Linear agent installation has its own webhook secret, access token, agent ID, and webhook URL.

```python
class LinearAdapter:
    def __init__(
        self,
        session_manager: Any,
        agent_name: str,
        webhook_secret: str,
        access_token: str,
        agent_id: str,
        route_path: str = "/webhooks/linear",
    ) -> None:
        self._session_manager = session_manager
        self._agent_name = agent_name
        self.service_name = f"linear:{agent_name}"
        self._webhook_secret = webhook_secret
        self._route_path = route_path
        self._api = LinearApiClient(access_token)
        ...
```

**Route registration** — use parameterized path and instance webhook secret:

```python
def register_routes(self, app: FastAPI) -> None:
    @app.post(self._route_path)
    async def handle_linear_webhook(request: Request) -> Response:
        ...
        if self._webhook_secret and not verify_signature(
            raw_body, signature, self._webhook_secret
        ):
            ...
```

**Session IDs** — Linear already provides unique session IDs per agent session (`payload.agent_session.id`), so these naturally don't collide between agents. No change needed to session ID construction.

**BridgeSessionRequest** — set agent_name:

```python
request = BridgeSessionRequest(
    external_session_id=session_id,
    service_name="linear",
    agent_name=self._agent_name,    # NEW
    prompt=prompt,
    ...
)
```

### 6. `app/main.py` — Create adapter instances per agent

```python
def _create_adapters(
    session_manager: SessionManager,
    github_auth_map: dict[str, GitHubAuth],  # keyed by agent_name
) -> list[ServiceAdapter]:
    adapters: list[ServiceAdapter] = []

    for agent_name, agent_config in settings.agents.items():
        is_default = agent_config.default

        for service in settings.enabled_services_list:
            if service == "slack":
                bot_token = settings.get_service_credential("SLACK_BOT_TOKEN", agent_name)
                app_token = settings.get_service_credential("SLACK_APP_TOKEN", agent_name)
                if bot_token and app_token:
                    adapters.append(SlackAdapter(
                        session_manager,
                        agent_name=agent_name,
                        bot_token=bot_token,
                        app_token=app_token,
                    ))

            elif service == "github":
                webhook_secret = settings.get_service_credential(
                    "GITHUB_WEBHOOK_SECRET", agent_name
                )
                route_path = "/webhooks/github" if is_default else f"/webhooks/github/{agent_name}"
                github_auth = github_auth_map.get(agent_name)
                if github_auth:
                    adapters.append(GitHubAdapter(
                        session_manager,
                        agent_name=agent_name,
                        webhook_secret=webhook_secret,
                        route_path=route_path,
                        auth=github_auth,
                    ))

            elif service == "linear":
                from app.services.linear.adapter import LinearAdapter

                webhook_secret = settings.get_service_credential(
                    "LINEAR_WEBHOOK_SECRET", agent_name
                )
                access_token = settings.get_service_credential(
                    "LINEAR_ACCESS_TOKEN", agent_name
                )
                agent_id = settings.get_service_credential(
                    "LINEAR_AGENT_ID", agent_name
                )
                route_path = "/webhooks/linear" if is_default else f"/webhooks/linear/{agent_name}"
                if access_token and agent_id:
                    adapters.append(LinearAdapter(
                        session_manager,
                        agent_name=agent_name,
                        webhook_secret=webhook_secret,
                        access_token=access_token,
                        agent_id=agent_id,
                        route_path=route_path,
                    ))

    return adapters
```

### 7. `app/core/repo_provider.py` — Agent-specific API keys

The `build_agent_env` method needs to know which agent's API key to forward. Currently it blindly forwards both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. With multi-agent, it should still forward both — the agent binary itself decides which key it needs. No change required here.

### 8. `.env.example` — Document the new config

```bash
# Agent registry (JSON)
# Each key is an agent name; "default: true" marks the primary agent.
# If not set, falls back to ACP_AGENT_COMMAND for a single-agent setup.
AGENTS_JSON='{"claude": {"command": "claude-code-acp", "default": true}, "codex": {"command": "codex-acp"}}'

# API keys (both agents may need both, depending on the binary)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Slack — default agent uses base vars
SLACK_BOT_TOKEN=xoxb-claude-bot-token
SLACK_APP_TOKEN=xapp-claude-app-token

# Slack — additional agents use __AGENTNAME suffix
SLACK_BOT_TOKEN__CODEX=xoxb-codex-bot-token
SLACK_APP_TOKEN__CODEX=xapp-codex-app-token

# GitHub — default agent
GITHUB_APP_ID=12345
GITHUB_PRIVATE_KEY="-----BEGIN RSA..."
GITHUB_WEBHOOK_SECRET=whsec_claude

# GitHub — additional agents
GITHUB_APP_ID__CODEX=67890
GITHUB_PRIVATE_KEY__CODEX="-----BEGIN RSA..."
GITHUB_WEBHOOK_SECRET__CODEX=whsec_codex

# Linear — default agent
LINEAR_WEBHOOK_SECRET=lin_whsec_claude
LINEAR_ACCESS_TOKEN=lin_api_claude
LINEAR_AGENT_ID=agent_claude_id

# Linear — additional agents
LINEAR_WEBHOOK_SECRET__CODEX=lin_whsec_codex
LINEAR_ACCESS_TOKEN__CODEX=lin_api_codex
LINEAR_AGENT_ID__CODEX=agent_codex_id
```

## What Doesn't Change

- **`app/acp/session.py`** — Already takes `command` as a constructor arg. No changes.
- **`app/acp/client.py`** — Agent-agnostic. No changes.
- **`app/core/update_router.py`** — Agent-agnostic. No changes.
- **`app/services/linear/adapter.py`** — Same parameterization as GitHub: receives agent name, credentials, and route path at construction time. See section 5.5 below.

## Backward Compatibility

If `AGENTS_JSON` is not set, the system falls back to the existing single-agent behavior:
- Uses `ACP_AGENT_COMMAND` (default: `claude-code-acp`)
- Creates one adapter per service with the existing env vars
- Session IDs don't include an agent name suffix (or use `":default"`)

This means existing deployments keep working with zero config changes.

## Implementation Order

### Phase 1: Core plumbing (no behavior change)

1. Add `AgentConfig` and `agents` property to `app/config.py`
2. Add `agent_name` to `BridgeSessionRequest` and `ActiveSession`
3. Update `SessionManager` to read agent command from request instead of global config
4. Update persistence (`to_dict`/`from_dict`) to include `agent_name`

At this point, everything works as before — just using `"default"` as the agent name.

### Phase 2: Parameterize adapters

5. Refactor `SlackAdapter.__init__` to accept agent name + credentials
6. Refactor `GitHubAdapter.__init__` to accept agent name + credentials + route path
7. Update session ID construction to include agent name
8. Update `_create_adapters` in `main.py` to loop over agents

### Phase 3: Config and docs

9. Add `AGENTS_JSON` and per-agent credential support to config
10. Update `.env.example` with multi-agent config
11. Update `CLAUDE.md` architecture docs

### Phase 4: Refinements

12. Handle the "untagged follow-up in a multi-agent thread" case (Slack)
13. Support separate `GitHubAuth` instances per agent (separate App IDs/keys)
14. Test cross-agent workflows (Claude makes PR, Codex reviews)

## Open Questions

1. **Same branch or different branches?** When Codex reviews Claude's PR, should it check out Claude's branch or create its own? Probably check out Claude's branch (read-only review), but this needs a `RepoProvider` mode for "use existing branch without creating a new one."

2. **Shared context?** Should Codex see Claude's conversation history when asked to review? Probably not — each agent has its own ACP session. But the user can paste relevant context in their @mention.

3. **Linear multi-agent?** Linear's agent model assigns work to a specific agent. Supporting multiple agents there would mean multiple Linear agent IDs and routing based on which agent Linear assigned. This is a separate design problem and can be deferred.

4. **Cost / resource management?** Two concurrent agents means two subprocesses, two sets of API calls. Worth monitoring but not a blocker.
