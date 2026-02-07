"""Receives ACP session updates and converts them to BridgeUpdates for service adapters.

Includes debouncing: buffers text chunks and flushes periodically or on tool call boundaries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

from app.core.types import BridgeUpdate

if TYPE_CHECKING:
    from app.acp.client import SessionUpdate
    from app.core.types import ServiceAdapter

logger = logging.getLogger(__name__)

# How long to buffer text chunks before flushing
DEBOUNCE_INTERVAL = 2.0  # seconds


class UpdateRouter:
    """Routes ACP session updates to the appropriate service adapter.

    Buffers agent message text chunks and flushes them periodically to avoid
    overwhelming the external service with per-token updates.
    """

    def __init__(self, adapter: ServiceAdapter, external_session_id: str):
        self._adapter = adapter
        self._external_session_id = external_session_id
        self._message_buffer = ""
        self._thought_buffer = ""
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def handle_update(self, session_id: str, update: SessionUpdate) -> None:
        """Process a single ACP session update."""
        if isinstance(update, AgentThoughtChunk):
            await self._handle_thought(update)
        elif isinstance(update, AgentMessageChunk):
            await self._handle_message(update)
        elif isinstance(update, ToolCallStart):
            await self._handle_tool_call_start(update)
        elif isinstance(update, ToolCallProgress):
            await self._handle_tool_call_progress(update)
        elif isinstance(update, AgentPlanUpdate):
            await self._handle_plan(update)

    async def _handle_thought(self, update: AgentThoughtChunk) -> None:
        if isinstance(update.content, TextContentBlock):
            self._thought_buffer += update.content.text
            self._ensure_flush_scheduled()

    async def _handle_message(self, update: AgentMessageChunk) -> None:
        if isinstance(update.content, TextContentBlock):
            self._message_buffer += update.content.text
            self._ensure_flush_scheduled()

    async def _handle_tool_call_start(self, update: ToolCallStart) -> None:
        # Flush any buffered text before reporting the tool call
        await self.flush()

        title = update.title or "Tool call"
        kind = update.kind or "other"
        metadata: dict[str, Any] = {
            "tool_call_id": update.tool_call_id,
            "kind": kind,
        }
        if update.locations:
            metadata["locations"] = [loc.path for loc in update.locations if loc.path]

        await self._adapter.send_update(
            self._external_session_id,
            BridgeUpdate(type="tool_call", content=title, metadata=metadata),
        )

    async def _handle_tool_call_progress(self, update: ToolCallProgress) -> None:
        # We only forward significant progress (completed tool calls with results)
        if update.status == "completed" and update.title:
            metadata: dict[str, Any] = {
                "tool_call_id": update.tool_call_id,
                "kind": update.kind or "other",
                "status": "completed",
            }
            await self._adapter.send_update(
                self._external_session_id,
                BridgeUpdate(type="tool_call", content=update.title, metadata=metadata),
            )

    async def _handle_plan(self, update: AgentPlanUpdate) -> None:
        await self.flush()

        entries = [{"content": entry.content, "status": entry.status} for entry in update.entries]
        await self._adapter.send_update(
            self._external_session_id,
            BridgeUpdate(type="plan", content="Plan updated", metadata={"entries": entries}),
        )

    def _ensure_flush_scheduled(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(DEBOUNCE_INTERVAL)
        await self.flush()

    async def flush(self) -> None:
        """Flush any buffered text to the adapter."""
        async with self._lock:
            if self._thought_buffer:
                text = self._thought_buffer
                self._thought_buffer = ""
                await self._adapter.send_update(
                    self._external_session_id,
                    BridgeUpdate(type="thought", content=text),
                )

            if self._message_buffer:
                text = self._message_buffer
                self._message_buffer = ""
                await self._adapter.send_update(
                    self._external_session_id,
                    BridgeUpdate(type="message_chunk", content=text),
                )

            # Cancel pending flush if we just flushed manually
            if self._flush_task is not None and not self._flush_task.done():
                self._flush_task.cancel()
                self._flush_task = None
