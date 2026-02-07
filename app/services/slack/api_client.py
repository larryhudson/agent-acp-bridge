"""Async Slack Web API client."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SlackApiClient:
    """Async client for Slack Web API.

    Handles:
    - Getting WebSocket URLs for Socket Mode
    - Posting and updating messages
    - Adding reactions
    """

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token
        self._client = httpx.AsyncClient(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=30.0,
        )

    async def get_websocket_url(self, app_token: str) -> str:
        """Get WebSocket URL from apps.connections.open.

        Args:
            app_token: App-level token (xapp-...)

        Returns:
            WebSocket URL for Socket Mode connection

        Raises:
            RuntimeError: If API call fails
        """
        response = await self._client.post(
            "/apps.connections.open",
            headers={"Authorization": f"Bearer {app_token}"},
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            error = data.get("error", "unknown")
            raise RuntimeError(f"Failed to get WebSocket URL: {error}")

        return data["url"]

    async def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Post a message to a channel or thread.

        Args:
            channel: Channel ID (e.g., C123456)
            text: Message text (used as fallback if blocks provided)
            thread_ts: Thread timestamp to reply to (optional)
            blocks: Block Kit blocks for rich formatting (optional)

        Returns:
            API response with message details including 'ts' (message timestamp)
        """
        payload: dict[str, Any] = {
            "channel": channel,
            "text": text,
        }

        if thread_ts:
            payload["thread_ts"] = thread_ts

        if blocks:
            payload["blocks"] = blocks

        response = await self._client.post("/chat.postMessage", json=payload)
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            error = data.get("error", "unknown")
            logger.error("Failed to post message: %s", error)
            raise RuntimeError(f"Failed to post message: {error}")

        return data

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Update an existing message.

        Args:
            channel: Channel ID
            ts: Message timestamp to update
            text: New message text
            blocks: New Block Kit blocks (optional)

        Returns:
            API response
        """
        payload: dict[str, Any] = {
            "channel": channel,
            "ts": ts,
            "text": text,
        }

        if blocks:
            payload["blocks"] = blocks

        response = await self._client.post("/chat.update", json=payload)
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            error = data.get("error", "unknown")
            logger.error("Failed to update message: %s", error)
            raise RuntimeError(f"Failed to update message: {error}")

        return data

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        """Add a reaction emoji to a message.

        Args:
            channel: Channel ID
            ts: Message timestamp
            emoji: Emoji name without colons (e.g., "white_check_mark")
        """
        payload = {
            "channel": channel,
            "timestamp": ts,
            "name": emoji,
        }

        response = await self._client.post("/reactions.add", json=payload)
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            error = data.get("error", "unknown")
            # Don't raise for already_reacted - it's not critical
            if error != "already_reacted":
                logger.warning("Failed to add reaction: %s", error)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
