from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_file": ".env", "extra": "ignore"}

    # Core
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    acp_agent_command: str = "claude-code-acp"  # or "codex-acp"
    enabled_services: str = "linear"  # comma-separated: "linear" or "linear,slack"
    bridge_base_url: str = ""  # e.g. "https://bridge.example.com" — for session viewer links

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


settings = Settings()
