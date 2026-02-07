"""GitHub App JWT authentication and installation token management."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
import jwt

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class InstallationToken:
    """A cached GitHub App installation token."""

    token: str
    expires_at: float  # UNIX timestamp

    @property
    def is_expired(self) -> bool:
        """Check if the token is expired (with 5-minute refresh margin)."""
        return time.time() >= self.expires_at - 300


class GitHubAuth:
    """Manages GitHub App JWT generation and installation token caching."""

    def __init__(self) -> None:
        self._token_cache: dict[int, InstallationToken] = {}
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Accept": "application/vnd.github+json"},
            timeout=30.0,
        )
        self._app_slug: str | None = None

    def _generate_jwt(self) -> str:
        """Generate a JWT signed with the App's private key (RS256, 9-minute expiry)."""
        now = int(time.time())
        payload = {
            "iat": now - 60,  # Issued 60s ago to account for clock drift
            "exp": now + 540,  # 9 minutes
            "iss": settings.github_app_id,
        }
        return jwt.encode(
            payload,
            settings.github_private_key_bytes,
            algorithm="RS256",
        )

    async def get_installation_token(self, installation_id: int) -> str:
        """Get an installation token, using cache if still valid."""
        cached = self._token_cache.get(installation_id)
        if cached and not cached.is_expired:
            return cached.token

        token_jwt = self._generate_jwt()
        response = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {token_jwt}"},
        )
        response.raise_for_status()
        data = response.json()

        # GitHub returns expires_at as ISO 8601; parse to timestamp
        # But we can also just cache for 55 minutes (tokens last 1 hour)
        self._token_cache[installation_id] = InstallationToken(
            token=data["token"],
            expires_at=time.time() + 3300,  # 55 minutes
        )

        logger.info("Obtained new installation token for installation %d", installation_id)
        return data["token"]

    async def get_app_slug(self) -> str:
        """Get the app's slug by calling GET /app. Cached after first call."""
        if self._app_slug is not None:
            return self._app_slug

        token_jwt = self._generate_jwt()
        response = await self._client.get(
            "/app",
            headers={"Authorization": f"Bearer {token_jwt}"},
        )
        response.raise_for_status()
        data = response.json()
        self._app_slug = data["slug"]
        logger.info("Detected GitHub App slug: %s", self._app_slug)
        return self._app_slug

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
