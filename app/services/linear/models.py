"""Pydantic models for Linear webhook payloads."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LinearIssue(BaseModel):
    id: str
    identifier: str | None = None
    title: str | None = None
    team_id: str | None = Field(None, alias="teamId")


class LinearComment(BaseModel):
    id: str
    body: str | None = None


class LinearAgentActivityContent(BaseModel):
    type: str | None = None
    body: str | None = None


class LinearAgentActivity(BaseModel):
    id: str | None = None
    body: str | None = None
    signal: str | None = None
    signal_metadata: dict | None = Field(None, alias="signalMetadata")
    content: LinearAgentActivityContent | None = None


class LinearAgentSession(BaseModel):
    id: str
    issue: LinearIssue | None = None
    comment: LinearComment | None = None


class AgentSessionEventPayload(BaseModel):
    """Payload for AgentSessionEvent webhooks."""

    type: str  # "AgentSessionEvent"
    action: str  # "created" | "prompted"
    agent_session: LinearAgentSession | None = Field(None, alias="agentSession")
    agent_activity: LinearAgentActivity | None = Field(None, alias="agentActivity")
    prompt_context: str | None = Field(None, alias="promptContext")
    previous_comments: list[LinearComment] | None = Field(None, alias="previousComments")
    guidance: str | None = None
    organization_id: str | None = Field(None, alias="organizationId")
    webhook_timestamp: int | None = Field(None, alias="webhookTimestamp")
    webhook_id: str | None = Field(None, alias="webhookId")


class PlanStep(BaseModel):
    content: str
    status: str = "pending"  # "pending" | "inProgress" | "completed" | "canceled"
