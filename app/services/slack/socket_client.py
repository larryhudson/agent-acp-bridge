"""Slack Socket Mode WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from app.services.slack.api_client import SlackApiClient
from app.services.slack.models import EventEnvelope

logger = logging.getLogger(__name__)


class SlackSocketClient:
    """Manages Slack Socket Mode WebSocket connection.

    Handles:
    - Obtaining WebSocket URL from apps.connections.open
    - Establishing and maintaining WebSocket connection
    - Receiving events and acknowledging them
    - Auto-reconnect on disconnect
    - Graceful shutdown
    """

    def __init__(
        self,
        app_token: str,
        bot_token: str,
        on_event: Callable[[EventEnvelope], Awaitable[None]],
    ) -> None:
        """Initialize Socket Mode client.

        Args:
            app_token: App-level token (xapp-...) for Socket Mode
            bot_token: Bot token for Web API calls
            on_event: Async callback to handle incoming events
        """
        self._app_token = app_token
        self._api = SlackApiClient(bot_token)
        self._on_event = on_event
        self._ws: ClientConnection | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        """Start the WebSocket client as a background task."""
        if self._running:
            logger.warning("Socket Mode client already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Started Slack Socket Mode client")

    async def stop(self) -> None:
        """Stop the WebSocket client and clean up."""
        if not self._running:
            return

        logger.info("Stopping Slack Socket Mode client")
        self._running = False

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                logger.warning("Socket Mode client task did not complete in time")
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        """Main connection loop with auto-reconnect."""
        reconnect_delay = 5

        while self._running:
            try:
                await self._connect_and_listen()
                # If we get here, connection closed cleanly
                reconnect_delay = 5
            except Exception:
                logger.exception("Socket Mode connection error")

            if self._running:
                logger.info("Reconnecting in %ds...", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                # Exponential backoff, max 60s
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _connect_and_listen(self) -> None:
        """Connect to WebSocket and listen for events."""
        # Get WebSocket URL
        try:
            ws_url = await self._api.get_websocket_url(self._app_token)
            logger.info("Connecting to Slack Socket Mode: %s...", ws_url[:50])
        except Exception:
            logger.exception("Failed to get WebSocket URL")
            raise

        # Connect
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                self._ws = ws
                logger.info("Connected to Slack Socket Mode")

                # Listen for messages
                async for message in ws:
                    if not self._running:
                        break

                    try:
                        data = json.loads(message)
                        await self._handle_message(data)
                    except Exception:
                        logger.exception("Error handling message: %s", message[:200])

        except ConnectionClosed as e:
            logger.warning("WebSocket connection closed: %s", e)
            raise
        except Exception:
            logger.exception("WebSocket error")
            raise
        finally:
            self._ws = None

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Handle incoming WebSocket message."""
        msg_type = data.get("type")

        if msg_type == "hello":
            logger.info("Received hello from Slack: %s connections", data.get("num_connections"))
            return

        if msg_type == "disconnect":
            reason = data.get("reason", "unknown")
            logger.warning("Disconnect requested: %s", reason)
            return

        # Event envelope — must acknowledge
        if "envelope_id" in data:
            try:
                envelope = EventEnvelope.model_validate(data)

                # Acknowledge immediately (Slack requires < 3 seconds)
                await self._acknowledge(envelope.envelope_id)

                # Process event asynchronously (don't block acknowledgment)
                task = asyncio.create_task(self._on_event(envelope))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            except Exception:
                logger.exception("Error processing envelope: %s", data)
                # Still acknowledge to avoid retries
                if envelope_id := data.get("envelope_id"):
                    await self._acknowledge(envelope_id)

    async def _acknowledge(self, envelope_id: str) -> None:
        """Acknowledge an event envelope.

        Args:
            envelope_id: The envelope ID to acknowledge
        """
        if not self._ws:
            logger.warning("Cannot acknowledge, WebSocket not connected")
            return

        try:
            ack = json.dumps({"envelope_id": envelope_id, "payload": {}})
            await self._ws.send(ack)
            logger.debug("Acknowledged envelope: %s", envelope_id)
        except Exception:
            logger.exception("Failed to acknowledge envelope: %s", envelope_id)
