from __future__ import annotations

import json

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_file": ".env", "extra": "ignore"}

    # Core
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    acp_agent_command: str = "claude-code-acp"  # or "codex-acp"
    enabled_services: str = "linear"  # comma-separated: "linear" or "linear,slack"
    project_mappings_json: str = "{}"  # JSON: {"linear:team_xxx": "/data/projects/my-app"}

    # Linear
    linear_webhook_secret: str = ""
    linear_access_token: str = ""
    linear_agent_id: str = ""

    # Slack
    slack_app_token: str = ""  # App-level token (xapp-...) for Socket Mode
    slack_bot_token: str = ""  # Bot token (xoxb-...) for Web API

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
    def project_mappings(self) -> dict[str, str]:
        return json.loads(self.project_mappings_json)

    def get_cwd_for_key(self, key: str) -> str | None:
        """Look up the working directory for a service-specific key.

        Keys follow the pattern 'service:identifier', e.g. 'linear:team_abc'.
        """
        return self.project_mappings.get(key)


settings = Settings()
