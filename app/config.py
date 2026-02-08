from __future__ import annotations

import json
import os

from pydantic import BaseModel
from pydantic_settings import BaseSettings


class AgentConfig(BaseModel):
    """Configuration for a single ACP agent."""

    command: str  # e.g. "claude-code-acp", "codex-acp"
    default: bool = False  # Is this the default agent?


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_file": ".env", "extra": "ignore"}

    # Core
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    acp_agent_command: str = "claude-code-acp"  # Legacy single-agent config
    enabled_services: str = "linear"  # comma-separated: "linear" or "linear,slack"

    # Multi-agent registry (JSON string)
    # e.g. '{"claude": {"command": "claude-code-acp", "default": true}, "codex": {"command": "codex-acp"}}'
    agents_json: str = ""

    # Shared repo config
    github_repo: str = ""  # e.g. "owner/repo"
    github_installation_id: int = 0  # GitHub App installation ID for non-webhook sessions

    # Linear
    linear_webhook_secret: str = ""
    linear_access_token: str = ""
    linear_agent_id: str = ""

    # Slack
    slack_app_token: str = ""  # App-level token (xapp-...) for Socket Mode
    slack_bot_token: str = ""  # Bot token (xoxb-...) for Web API
    slack_user_token: str = ""  # User token (xoxp-...) for search API

    # GitHub
    github_app_id: str = ""
    github_private_key: str = ""  # PEM key with \n for newlines
    github_webhook_secret: str = ""
    github_bot_login: str = ""  # Optional fallback if auto-detect fails

    @property
    def github_private_key_bytes(self) -> bytes:
        """Convert the PEM key string (with escaped newlines) to bytes."""
        return self.github_private_key.replace("\\n", "\n").encode("utf-8")

    @property
    def enabled_services_list(self) -> list[str]:
        return [s.strip() for s in self.enabled_services.split(",") if s.strip()]

    @property
    def agents(self) -> dict[str, AgentConfig]:
        """Parse agent registry from JSON config.

        If AGENTS_JSON is not set, falls back to a single agent from ACP_AGENT_COMMAND.
        """
        if not self.agents_json:
            return {"default": AgentConfig(command=self.acp_agent_command, default=True)}
        raw = json.loads(self.agents_json)
        return {name: AgentConfig(**cfg) for name, cfg in raw.items()}

    @property
    def default_agent_name(self) -> str:
        """Return the name of the default agent."""
        for name, cfg in self.agents.items():
            if cfg.default:
                return name
        return next(iter(self.agents))

    def get_agent_config(self, agent_name: str) -> AgentConfig:
        """Get config for a specific agent. Falls back to default if name is empty."""
        if not agent_name:
            agent_name = self.default_agent_name
        return self.agents[agent_name]

    def get_service_credential(self, var_name: str, agent_name: str) -> str:
        """Get a service credential, checking agent-specific override first.

        For the default agent, uses the base env var (e.g. SLACK_BOT_TOKEN).
        For other agents, checks SLACK_BOT_TOKEN__CODEX first, falls back to base.
        """
        if agent_name == self.default_agent_name:
            return getattr(self, var_name.lower(), "")

        # Check for agent-specific override (e.g. SLACK_BOT_TOKEN__CODEX)
        suffixed = f"{var_name}__{agent_name}".upper()
        value = os.environ.get(suffixed, "")
        if value:
            return value

        # Fall back to base
        return getattr(self, var_name.lower(), "")


settings = Settings()
