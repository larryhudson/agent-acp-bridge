"""FastAPI app assembly — lifespan, adapter registration, health check."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.core.repo_provider import RepoProvider
from app.core.session_manager import SessionManager
from app.core.types import ServiceAdapter
from app.services.github.auth import GitHubAuth
from app.session_viewer.router import router as session_viewer_router

logger = logging.getLogger(__name__)

# Module-level references for access during requests
_session_manager: SessionManager | None = None
_adapters: list[ServiceAdapter] = []
_cleanup_task: asyncio.Task[None] | None = None


async def _periodic_cleanup_task(
    repo_provider: RepoProvider, session_manager: SessionManager
) -> None:
    """Background task that periodically cleans up stale worktrees."""
    if not settings.worktree_cleanup_enabled:
        logger.info("Worktree cleanup is disabled")
        return

    interval_seconds = settings.worktree_cleanup_interval_hours * 3600
    logger.info(
        "Starting periodic worktree cleanup (threshold: %d days, interval: %d hours)",
        settings.worktree_cleanup_age_days,
        settings.worktree_cleanup_interval_hours,
    )

    while True:
        try:
            await asyncio.sleep(interval_seconds)

            # Get active session cwds to avoid cleaning them
            active_cwds = {
                session.cwd for session in session_manager._active_sessions.values()
            }

            logger.debug("Running periodic worktree cleanup...")
            cleaned = await repo_provider.cleanup_stale_worktrees(
                age_threshold_days=settings.worktree_cleanup_age_days,
                active_sessions=active_cwds,
            )
            if cleaned > 0:
                logger.info("Periodic cleanup removed %d stale worktree(s)", cleaned)
        except asyncio.CancelledError:
            logger.info("Periodic cleanup task cancelled")
            break
        except Exception:
            logger.exception("Error in periodic cleanup task")


def _create_adapters(
    session_manager: SessionManager,
    github_auth_map: dict[str, GitHubAuth] | None = None,
) -> list[ServiceAdapter]:
    """Instantiate adapters for all enabled services, one per agent.

    For each agent in the registry, creates adapter instances for each
    enabled service with agent-specific credentials.
    """
    adapters: list[ServiceAdapter] = []
    github_auth_map = github_auth_map or {}

    for agent_name, agent_config in settings.agents.items():
        is_default = agent_config.default

        for service in settings.enabled_services_list:
            if service == "slack":
                from app.services.slack.adapter import SlackAdapter

                bot_token = settings.get_service_credential("SLACK_BOT_TOKEN", agent_name)
                app_token = settings.get_service_credential("SLACK_APP_TOKEN", agent_name)
                if bot_token and app_token:
                    adapters.append(
                        SlackAdapter(
                            session_manager,
                            agent_name=agent_name,
                            bot_token=bot_token,
                            app_token=app_token,
                        )
                    )
                else:
                    logger.warning(
                        "Slack tokens missing for agent %s — skipping Slack adapter",
                        agent_name,
                    )

            elif service == "github":
                from app.services.github.adapter import GitHubAdapter

                webhook_secret = settings.get_service_credential(
                    "GITHUB_WEBHOOK_SECRET", agent_name
                )
                bot_login = settings.get_service_credential("GITHUB_BOT_LOGIN", agent_name)
                route_path = "/webhooks/github" if is_default else f"/webhooks/github/{agent_name}"
                github_auth = github_auth_map.get(agent_name)
                adapters.append(
                    GitHubAdapter(
                        session_manager,
                        agent_name=agent_name,
                        webhook_secret=webhook_secret,
                        route_path=route_path,
                        auth=github_auth,
                        bot_login=bot_login,
                    )
                )

            elif service == "linear":
                from app.services.linear.adapter import LinearAdapter

                access_token = settings.get_service_credential("LINEAR_ACCESS_TOKEN", agent_name)
                webhook_secret = settings.get_service_credential(
                    "LINEAR_WEBHOOK_SECRET", agent_name
                )
                route_path = "/webhooks/linear" if is_default else f"/webhooks/linear/{agent_name}"
                if access_token:
                    adapters.append(
                        LinearAdapter(
                            session_manager,
                            agent_name=agent_name,
                            access_token=access_token,
                            webhook_secret=webhook_secret,
                            route_path=route_path,
                        )
                    )
                else:
                    logger.warning(
                        "Linear access token missing for agent %s — skipping",
                        agent_name,
                    )

            else:
                logger.warning("Unknown service: %s (skipping)", service)

    return adapters


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: initialize session manager and adapters, clean up on shutdown."""
    global _session_manager, _adapters, _cleanup_task

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Build per-agent GitHub auth instances
    github_auth_map: dict[str, GitHubAuth] = {}
    default_github_auth: GitHubAuth | None = None
    if settings.github_repo:
        for agent_name, _agent_cfg in settings.agents.items():
            app_id = settings.get_service_credential("GITHUB_APP_ID", agent_name)
            private_key = settings.get_service_credential("GITHUB_PRIVATE_KEY", agent_name)
            if app_id and private_key:
                auth = GitHubAuth(app_id=app_id, private_key=private_key)
                github_auth_map[agent_name] = auth
                if default_github_auth is None:
                    default_github_auth = auth

    # Create shared RepoProvider (default auth for clone/fetch, auth_map for per-agent GH_TOKEN)
    repo_provider = RepoProvider(
        auth=default_github_auth,
        auth_map=github_auth_map,
        enabled_services=settings.enabled_services_list,
    )

    _session_manager = SessionManager(repo_provider=repo_provider)
    _adapters = _create_adapters(_session_manager, github_auth_map=github_auth_map)

    # Startup cleanup: remove stale worktrees that aren't in active sessions
    if settings.worktree_cleanup_enabled:
        logger.info("Running startup worktree cleanup...")
        try:
            cleaned = await repo_provider.cleanup_stale_worktrees(
                age_threshold_days=settings.worktree_cleanup_age_days,
                active_sessions=set(),  # No active sessions yet at startup
            )
            if cleaned > 0:
                logger.info("Startup cleanup removed %d stale worktree(s)", cleaned)
        except Exception:
            logger.exception("Error during startup worktree cleanup")

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

    # Start background cleanup task
    if settings.worktree_cleanup_enabled:
        _cleanup_task = asyncio.create_task(
            _periodic_cleanup_task(repo_provider, _session_manager)
        )

    logger.info("ACP Bridge started (services: %s)", settings.enabled_services)

    yield

    # Shutdown
    logger.info("Shutting down ACP Bridge...")

    # Cancel background cleanup task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass

    await _session_manager.shutdown()

    for adapter in _adapters:
        await adapter.close()

    for auth in github_auth_map.values():
        await auth.close()

    _session_manager = None
    _adapters = []
    _cleanup_task = None


app = FastAPI(
    title="ACP Bridge",
    description="Service-agnostic bridge connecting external services to ACP agents",
    version="0.1.0",
    lifespan=lifespan,
)

# Session viewer web UI
app.include_router(session_viewer_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "services": settings.enabled_services,
    }
