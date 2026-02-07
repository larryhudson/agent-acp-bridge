"""Pydantic models for Slack events and payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SlackEvent(BaseModel):
    """Base Slack event."""

    type: str
    user: str | None = None
    channel: str | None = None
    ts: str | None = None


class AppMentionEvent(BaseModel):
    """app_mention event - triggered when bot is @mentioned."""

    type: Literal["app_mention"]
    user: str
    text: str
    channel: str
    ts: str
    thread_ts: str | None = None
    event_ts: str


class MessageEvent(BaseModel):
    """message event - triggered when message is posted."""

    type: Literal["message"]
    user: str | None = None
    text: str | None = None
    channel: str
    ts: str
    thread_ts: str | None = None
    subtype: str | None = None  # Filter out bot_message, etc.
    event_ts: str


class EventCallback(BaseModel):
    """Event callback payload."""

    token: str
    team_id: str
    api_app_id: str
    event: dict[str, Any]
    type: Literal["event_callback"]
    event_id: str
    event_time: int


class EventEnvelope(BaseModel):
    """Slack Socket Mode event envelope."""

    envelope_id: str
    type: str  # "events_api", "hello", "disconnect", etc.
    payload: dict[str, Any] = Field(default_factory=dict)
    accepts_response_payload: bool | None = None
    retry_attempt: int | None = None
    retry_reason: str | None = None


class HelloEvent(BaseModel):
    """Hello event sent when WebSocket connects."""

    type: Literal["hello"]
    num_connections: int
    debug_info: dict[str, Any]
    connection_info: dict[str, Any]


class DisconnectEvent(BaseModel):
    """Disconnect event sent before connection closes."""

    type: Literal["disconnect"]
    reason: str
    debug_info: dict[str, Any] | None = None
