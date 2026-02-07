"""LinearAdapter — implements ServiceAdapter for Linear's Agents API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, Request, Response

from app.config import settings
from app.core.repo_provider import slugify
from app.core.types import BridgeSessionRequest, BridgeUpdate
from app.services.linear.api_client import LinearApiClient
from app.services.linear.models import AgentSessionEventPayload, PlanStep
from app.services.linear.webhooks import verify_signature, verify_timestamp

logger = logging.getLogger(__name__)


class LinearAdapter:
    """Service adapter for Linear's Agents API.

    Handles:
    - Webhook reception and verification
    - Translating BridgeUpdates to Linear activity API calls
    - Session lifecycle management
    """

    service_name: str = "linear"

    def __init__(self, session_manager: Any) -> None:
        self._session_manager = session_manager
        self._api = LinearApiClient(settings.linear_access_token)
        # Track accumulated message text per session for final response
        self._message_buffers: dict[str, str] = {}
        # Keep references to background tasks so they aren't garbage collected
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """No background tasks needed for webhook-based adapter."""
        pass

    def register_routes(self, app: FastAPI) -> None:
        """Register the Linear webhook endpoint."""

        @app.post("/webhooks/linear")
        async def handle_linear_webhook(request: Request) -> Response:
            raw_body = await request.body()

            # Verify signature
            signature = request.headers.get("Linear-Signature", "")
            if settings.linear_webhook_secret and not verify_signature(
                raw_body, signature, settings.linear_webhook_secret
            ):
                logger.warning("Invalid webhook signature")
                return Response(status_code=400)

            payload = AgentSessionEventPayload.model_validate_json(raw_body)

            # Verify timestamp
            if not verify_timestamp(payload.webhook_timestamp):
                logger.warning("Webhook timestamp too old")
                return Response(status_code=400)

            # Must respond within 5 seconds — process asynchronously
            logger.info("Webhook received: type=%s action=%s", payload.type, payload.action)
            if payload.action == "prompted":
                logger.info("Raw prompted payload: %s", raw_body.decode())
            if payload.type == "AgentSessionEvent":
                if payload.action == "created":
                    task = asyncio.create_task(self._handle_created(payload))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                elif payload.action == "prompted":
                    task = asyncio.create_task(self._handle_prompted(payload))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)

            return Response(status_code=200)

    async def _handle_created(self, payload: AgentSessionEventPayload) -> None:
        """Handle a new agent session creation."""
        if not payload.agent_session:
            logger.warning("No agentSession in created payload")
            return

        session_id = payload.agent_session.id

        # Build prompt from promptContext or fallback
        prompt = payload.prompt_context or ""
        issue_title = ""
        if payload.agent_session.issue:
            issue_title = (
                payload.agent_session.issue.title or payload.agent_session.issue.identifier
            )
            if not prompt:
                prompt = f"Issue: {issue_title}"

        request = BridgeSessionRequest(
            external_session_id=session_id,
            service_name=self.service_name,
            prompt=prompt,
            descriptive_name=slugify(issue_title) if issue_title else "linear-task",
        )

        # Move issue to started state if applicable
        await self._maybe_start_issue(payload)

        await self._session_manager.handle_new_session(self, request)

    async def _handle_prompted(self, payload: AgentSessionEventPayload) -> None:
        """Handle a follow-up prompt on an existing session."""
        if not payload.agent_session:
            logger.warning("No agentSession in prompted payload")
            return

        session_id = payload.agent_session.id

        logger.info(
            "Prompted webhook for %s: agentActivity=%r, promptContext=%r",
            session_id,
            payload.agent_activity.model_dump() if payload.agent_activity else None,
            payload.prompt_context[:200] if payload.prompt_context else None,
        )

        # Check for stop signal
        if payload.agent_activity and payload.agent_activity.signal == "stop":
            await self._session_manager.handle_cancel(session_id)
            await self._api.create_activity(
                session_id,
                activity_type="response",
                body="Stopped as requested.",
            )
            return

        # Get the user's message — try content.body, then body, then promptContext
        prompt = ""
        if payload.agent_activity:
            if payload.agent_activity.content and payload.agent_activity.content.body:
                prompt = payload.agent_activity.content.body
            elif payload.agent_activity.body:
                prompt = payload.agent_activity.body
        if not prompt and payload.prompt_context:
            prompt = payload.prompt_context

        if not prompt:
            logger.warning("Empty prompt in prompted webhook for %s", session_id)
            return

        await self._session_manager.handle_followup(session_id, prompt)

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        """Not used directly — webhooks handle this via _handle_created."""
        raise NotImplementedError

    async def send_update(self, session_id: str, update: BridgeUpdate) -> None:
        """Translate a BridgeUpdate into Linear activity API calls."""
        try:
            if update.type == "thought":
                await self._api.create_activity(
                    session_id,
                    activity_type="thought",
                    body=update.content,
                    ephemeral=True,
                )

            elif update.type == "message_chunk":
                # Accumulate message text for the final response
                self._message_buffers.setdefault(session_id, "")
                self._message_buffers[session_id] += update.content

            elif update.type == "tool_call":
                action_name = update.content
                parameter = ""
                if locations := update.metadata.get("locations"):
                    parameter = ", ".join(locations)

                await self._api.create_activity(
                    session_id,
                    activity_type="action",
                    action=action_name,
                    parameter=parameter or None,
                    ephemeral=True,
                )

            elif update.type == "plan":
                entries = update.metadata.get("entries", [])
                steps = []
                for entry in entries:
                    # Map ACP plan status to Linear plan status
                    status = entry.get("status", "pending")
                    linear_status = {
                        "pending": "pending",
                        "in_progress": "inProgress",
                        "completed": "completed",
                    }.get(status, "pending")
                    steps.append(PlanStep(content=entry["content"], status=linear_status))

                if steps:
                    await self._api.update_session_plan(session_id, steps)

        except Exception:
            logger.exception("Error sending update to Linear for %s", session_id)

    async def send_completion(self, session_id: str, message: str) -> None:
        """Send a completion response to Linear."""
        # Use accumulated message if available, otherwise use the provided message
        body = self._message_buffers.pop(session_id, "") or message

        try:
            await self._api.create_activity(
                session_id,
                activity_type="response",
                body=body,
            )
        except Exception:
            logger.exception("Error sending completion to Linear for %s", session_id)

    async def send_error(self, session_id: str, error: str) -> None:
        """Send an error activity to Linear."""
        self._message_buffers.pop(session_id, None)

        try:
            await self._api.create_activity(
                session_id,
                activity_type="error",
                body=error,
            )
        except Exception:
            logger.exception("Error sending error to Linear for %s", session_id)

    async def _maybe_start_issue(self, payload: AgentSessionEventPayload) -> None:
        """Move issue to first 'started' state if applicable."""
        if not payload.agent_session or not payload.agent_session.issue:
            return

        issue = payload.agent_session.issue
        if not issue.team_id:
            return

        try:
            started_state = await self._api.get_started_state(issue.team_id)
            if started_state:
                await self._api.update_issue_state(issue.id, started_state["id"])
        except Exception:
            logger.exception("Failed to update issue state for %s", issue.id)

    async def close(self) -> None:
        await self._api.close()
