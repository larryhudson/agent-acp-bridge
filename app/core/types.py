from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from fastapi import FastAPI


@dataclass
class BridgeSessionRequest:
    """A service-agnostic request to start or continue an agent session."""

    external_session_id: str  # e.g., Linear agent session ID
    service_name: str  # e.g., "linear", "slack"
    prompt: str  # The user's message / issue context
    descriptive_name: str = ""  # Used for branch naming (e.g. issue title)
    is_followup: bool = False  # Whether this continues an existing session
    service_metadata: dict[str, Any] | None = None  # Adapter-specific state to persist


@dataclass
class BridgeUpdate:
    """A service-agnostic update from the agent."""

    type: Literal["thought", "action", "message_chunk", "plan", "tool_call"]
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ServiceAdapter(Protocol):
    """Interface that each external service must implement.

    This protocol supports both webhook-based and socket-based adapters:
    - Webhook adapters (e.g., Linear): implement register_routes() and on_session_created()
    - Socket adapters (e.g., Slack): implement start() for background connection management

    Methods marked as "optional" can be no-ops if not needed by the adapter type.
    """

    service_name: str

    def register_routes(self, app: FastAPI) -> None:
        """Register webhook/event routes on the FastAPI app.

        Optional: No-op for socket-based adapters that don't use HTTP routes.
        """
        ...

    async def start(self) -> None:
        """Start any background tasks (e.g., WebSocket connections).

        Optional: No-op for webhook-based adapters.
        Called during app startup after routes are registered.
        """
        ...

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        """Parse an incoming event into a service-agnostic session request.

        Only used by webhook-based adapters. Socket-based adapters should
        raise NotImplementedError or make this a no-op.
        """
        ...

    async def send_update(self, session_id: str, update: BridgeUpdate) -> None:
        """Send a bridge update back to the external service.

        Required: Each adapter translates BridgeUpdate into its own API calls.
        """
        ...

    async def send_completion(self, session_id: str, message: str, session_url: str = "") -> None:
        """Signal that the agent has completed its work.

        Required: Send final response/completion message to the external service.
        session_url: Optional link to the session viewer for inspecting the full session.
        """
        ...

    async def send_error(self, session_id: str, error: str) -> None:
        """Signal that the agent encountered an error.

        Required: Send error message to the external service.
        """
        ...

    async def close(self) -> None:
        """Clean up resources (API clients, WebSocket connections, etc.).

        Optional: No-op if no cleanup needed.
        Called during app shutdown.
        """
        ...
