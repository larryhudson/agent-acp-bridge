"""FastAPI app assembly — lifespan, adapter registration, health check."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.core.session_manager import SessionManager
from app.core.types import ServiceAdapter

logger = logging.getLogger(__name__)

# Module-level references for access during requests
_session_manager: SessionManager | None = None
_adapters: list[ServiceAdapter] = []


def _create_adapters(session_manager: SessionManager) -> list[ServiceAdapter]:
    """Instantiate adapters for all enabled services."""
    adapters: list[ServiceAdapter] = []

    for service in settings.enabled_services_list:
        if service == "linear":
            from app.services.linear.adapter import LinearAdapter

            adapters.append(LinearAdapter(session_manager))
        elif service == "slack":
            from app.services.slack.adapter import SlackAdapter

            adapters.append(SlackAdapter(session_manager))
        elif service == "github":
            from app.services.github.adapter import GitHubAdapter

            adapters.append(GitHubAdapter(session_manager))
        else:
            logger.warning("Unknown service: %s (skipping)", service)

    return adapters


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: initialize session manager and adapters, clean up on shutdown."""
    global _session_manager, _adapters

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _session_manager = SessionManager()
    _adapters = _create_adapters(_session_manager)

    # Register routes and start each adapter
    for adapter in _adapters:
        adapter.register_routes(app)
        # Restore any persisted sessions BEFORE starting (to avoid race condition)
        _session_manager.restore_sessions_for_adapter(adapter)
        restore_fn = getattr(adapter, "restore_persisted_sessions", None)
        if restore_fn is not None:
            restore_fn()
        # Now start the adapter (begins receiving events)
        await adapter.start()
        logger.info("Started adapter: %s", adapter.service_name)

    logger.info("ACP Bridge started (services: %s)", settings.enabled_services)

    yield

    # Shutdown
    logger.info("Shutting down ACP Bridge...")
    await _session_manager.shutdown()

    for adapter in _adapters:
        await adapter.close()

    _session_manager = None
    _adapters = []


app = FastAPI(
    title="ACP Bridge",
    description="Service-agnostic bridge connecting external services to ACP agents",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "services": settings.enabled_services,
    }
