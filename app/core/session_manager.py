"""SessionManager — orchestrates the lifecycle of bridge sessions.

Maps external service sessions to ACP sessions, handles new sessions,
follow-ups, cancellations, and cleanup.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.acp.session import AcpSession
from app.config import settings
from app.core.repo_provider import RepoProvider
from app.core.types import BridgeSessionRequest, BridgeUpdate, ServiceAdapter
from app.core.update_router import UpdateRouter

_VIEWER_URL_TEMPLATE = "{base_url}/sessions/{session_id}"

logger = logging.getLogger(__name__)


@dataclass
class ActiveSession:
    """Tracks an active bridge session."""

    external_session_id: str
    service_name: str
    adapter: ServiceAdapter
    acp_session: AcpSession | None  # None when restored from persistence
    update_router: UpdateRouter | None  # None when restored from persistence
    acp_session_id: str  # The actual ACP session ID for resumption
    cwd: str  # Working directory for this session
    branch_name: str = ""  # Git branch for this session
    agent_name: str = ""  # Which agent is running this session
    service_metadata: dict[str, Any] | None = None  # Adapter-specific state
    github_repo: str = ""  # Repo used for this session (for follow-ups)
    github_installation_id: int = 0  # Installation ID used (for follow-ups)
    system_prompt: str = ""  # Instruction-level context for the agent

    def to_dict(self) -> dict[str, Any]:
        """Serialize session metadata to dict (excluding runtime objects)."""
        data: dict[str, Any] = {
            "external_session_id": self.external_session_id,
            "service_name": self.service_name,
            "acp_session_id": self.acp_session_id,
            "cwd": self.cwd,
            "branch_name": self.branch_name,
            "agent_name": self.agent_name,
            "github_repo": self.github_repo,
            "github_installation_id": self.github_installation_id,
        }
        if self.service_metadata:
            data["service_metadata"] = self.service_metadata
        if self.system_prompt:
            data["system_prompt"] = self.system_prompt
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], adapter: ServiceAdapter) -> ActiveSession:
        """Restore session from persisted metadata (without active ACP session)."""
        return cls(
            external_session_id=data["external_session_id"],
            service_name=data["service_name"],
            adapter=adapter,
            acp_session=None,  # Will be created when needed
            update_router=None,  # Will be created when needed
            acp_session_id=data["acp_session_id"],
            cwd=data["cwd"],
            branch_name=data.get("branch_name", ""),
            agent_name=data.get("agent_name", ""),
            service_metadata=data.get("service_metadata"),
            github_repo=data.get("github_repo", ""),
            github_installation_id=data.get("github_installation_id", 0),
            system_prompt=data.get("system_prompt", ""),
        )


class SessionManager:
    """Orchestrates bridge sessions between external services and ACP agents."""

    def __init__(
        self,
        repo_provider: RepoProvider,
        persistence_file: Path = Path("/var/lib/bridge/sessions.json"),
    ) -> None:
        self._repo_provider = repo_provider
        self._active_sessions: dict[str, ActiveSession] = {}
        self._persistence_file = persistence_file
        self._persisted_metadata: dict[str, Any] = {}
        self._load_sessions()

    def _build_session_url(self, acp_session_id: str) -> str:
        """Build a viewer URL for the given ACP session, or return empty string."""
        if not settings.bridge_base_url:
            return ""
        base = settings.bridge_base_url.rstrip("/")
        return _VIEWER_URL_TEMPLATE.format(base_url=base, session_id=acp_session_id)

    def _load_sessions(self) -> None:
        """Load persisted session metadata from disk.

        Note: This only restores the metadata. Active ACP sessions and adapters
        are not restored - they will be recreated on first follow-up.
        """
        if not self._persistence_file.exists():
            logger.info("No persisted sessions file found at %s", self._persistence_file)
            return

        try:
            with open(self._persistence_file) as f:
                data = json.load(f)

            # We can't fully restore sessions without adapters, so just log what we found
            session_count = len(data.get("sessions", {}))
            if session_count > 0:
                logger.info(
                    "Found %d persisted session(s) in %s. Sessions will be available "
                    "for resumption when services reconnect.",
                    session_count,
                    self._persistence_file,
                )
                # Store the raw data for later use when adapters reconnect
                self._persisted_metadata = data.get("sessions", {})
            else:
                self._persisted_metadata = {}
        except Exception:
            logger.exception("Failed to load persisted sessions from %s", self._persistence_file)
            self._persisted_metadata = {}

    def _save_sessions(self) -> None:
        """Persist current session metadata to disk."""
        try:
            # Ensure parent directory exists
            self._persistence_file.parent.mkdir(parents=True, exist_ok=True)

            sessions_data = {
                external_id: session.to_dict()
                for external_id, session in self._active_sessions.items()
            }

            data = {"sessions": sessions_data}

            # Write atomically using a temp file
            temp_file = self._persistence_file.with_suffix(".tmp")
            with open(temp_file, "w") as f:
                json.dump(data, f, indent=2)
            temp_file.replace(self._persistence_file)

            logger.debug(
                "Persisted %d session(s) to %s", len(sessions_data), self._persistence_file
            )
        except Exception:
            logger.exception("Failed to persist sessions to %s", self._persistence_file)

    async def handle_new_session(
        self, adapter: ServiceAdapter, request: BridgeSessionRequest
    ) -> None:
        """Handle a new session request from a service adapter.

        1. Sends an immediate "thinking" update
        2. Prepares the repo (clone/fetch, create branch, install skills)
        3. Spawns an ACP session
        4. Wires up the update router
        5. Sends the prompt
        6. On completion, calls adapter.send_completion()
        """
        external_id = request.external_session_id

        # Send immediate acknowledgment
        await adapter.send_update(
            external_id,
            BridgeUpdate(type="thought", content="Starting work..."),
        )

        # Resolve agent name early — needed for both repo prep and ACP session
        agent_name = request.agent_name or settings.default_agent_name
        agent_config = settings.get_agent_config(agent_name)

        # Prepare the repo: clone/fetch, create branch, install skill files
        try:
            repo_session = await self._repo_provider.prepare_new_session(
                request.descriptive_name,
                agent_name=agent_name,
                github_repo=request.github_repo,
                github_installation_id=request.github_installation_id,
            )
        except Exception:
            logger.exception("Failed to prepare repo for %s", external_id)
            await adapter.send_error(external_id, "Failed to prepare repository")
            return

        # Create update router for this session
        router = UpdateRouter(adapter, external_id)
        acp_session = AcpSession(
            command=agent_config.command,
            on_update=router.handle_update,
            env=repo_session.env or None,
        )

        try:
            acp_session_id = await acp_session.start(cwd=repo_session.cwd)
        except Exception:
            logger.exception("Failed to start ACP session for %s", external_id)
            await adapter.send_error(external_id, "Failed to start agent session")
            return

        # Track the session
        active = ActiveSession(
            external_session_id=external_id,
            service_name=request.service_name,
            adapter=adapter,
            acp_session=acp_session,
            update_router=router,
            acp_session_id=acp_session_id,
            cwd=repo_session.cwd,
            branch_name=repo_session.branch_name,
            agent_name=agent_name,
            service_metadata=request.service_metadata,
            github_repo=request.github_repo,
            github_installation_id=request.github_installation_id,
        )
        self._active_sessions[external_id] = active
        self._save_sessions()  # Persist to disk

        # Append git/PR instructions when working on a branch
        prompt = request.prompt
        if repo_session.branch_name:
            prompt += (
                "\n\n---\n"
                f"You are working on a git branch: `{repo_session.branch_name}`. "
                "This branch has been automatically created with the latest changes "
                "from the main branch.\n"
                "If the user is asking you to make code changes, commit your changes, "
                "push the branch, and create a GitHub pull request using the `gh` CLI. "
                "The `GH_TOKEN` env var is already set.\n"
                "If the user is just asking questions or requesting information, "
                "do not make any changes or create a PR."
            )

        # Send the prompt and wait for completion
        session_url = self._build_session_url(acp_session_id)

        try:
            stop_reason = await acp_session.prompt(prompt, system_prompt=request.system_prompt)
            # Flush any remaining buffered updates
            await router.flush()

            if stop_reason == "end_turn":
                await adapter.send_completion(
                    external_id, "Work completed", session_url=session_url
                )
            elif stop_reason == "cancelled":
                await adapter.send_completion(
                    external_id, "Work was cancelled", session_url=session_url
                )
            else:
                await adapter.send_completion(
                    external_id,
                    f"Agent stopped (reason: {stop_reason})",
                    session_url=session_url,
                )
        except Exception:
            logger.exception("Error during ACP prompt for %s", external_id)
            await adapter.send_error(external_id, "Agent encountered an error during execution")
        finally:
            # Stop the subprocess but KEEP the ActiveSession record
            # This allows follow-ups to resume with the same acp_session_id
            await acp_session.stop()
            logger.info(
                "Stopped ACP subprocess for %s (session record kept for resumption)", external_id
            )

    async def handle_followup(self, external_session_id: str, prompt: str) -> None:
        """Handle a follow-up message on an existing session.

        Resumes the ACP session with full conversation history.
        """
        active = self._active_sessions.get(external_session_id)
        if active is None:
            logger.warning("No active session for follow-up: %s", external_session_id)
            return

        adapter = active.adapter

        # Send immediate acknowledgment
        await adapter.send_update(
            external_session_id,
            BridgeUpdate(type="thought", content="Processing follow-up..."),
        )

        # Prepare the repo for resumption (fetch, checkout branch, refresh tokens)
        env: dict[str, str] | None = None
        agent_name = active.agent_name
        if active.branch_name:
            try:
                repo_session = await self._repo_provider.prepare_resume_session(
                    active.branch_name,
                    cwd=active.cwd,
                    agent_name=agent_name,
                    github_repo=active.github_repo,
                    github_installation_id=active.github_installation_id,
                )
                env = repo_session.env or None
            except Exception:
                logger.exception("Failed to prepare repo for follow-up %s", external_session_id)
                # Fall back to just refreshing tokens
                try:
                    fresh_env = await self._repo_provider.build_agent_env(
                        agent_name=agent_name,
                        installation_id=active.github_installation_id,
                    )
                    env = fresh_env or None
                except Exception:
                    logger.exception("Failed to build agent env for %s", external_session_id)
        else:
            # No branch (legacy session) — just refresh tokens
            try:
                fresh_env = await self._repo_provider.build_agent_env(
                    agent_name=agent_name,
                    installation_id=active.github_installation_id,
                )
                env = fresh_env or None
            except Exception:
                logger.exception("Failed to build agent env for %s", external_session_id)

        # Create a new ACP subprocess and RESUME the session with full history
        router = UpdateRouter(adapter, external_session_id)
        agent_config = settings.get_agent_config(active.agent_name)
        acp_session = AcpSession(
            command=agent_config.command,
            on_update=router.handle_update,
            env=env,
        )

        try:
            # Resume the existing session (loads conversation history from disk)
            await acp_session.start(cwd=active.cwd, resume_session_id=active.acp_session_id)
            logger.info(
                "Resumed ACP session %s for follow-up (external: %s)",
                active.acp_session_id,
                external_session_id,
            )
        except Exception:
            logger.exception("Failed to resume ACP session for %s", external_session_id)
            await adapter.send_error(external_session_id, "Failed to resume session")
            return

        # Update the active session with new subprocess
        active.acp_session = acp_session
        active.update_router = router

        session_url = self._build_session_url(active.acp_session_id)

        try:
            stop_reason = await acp_session.prompt(prompt, system_prompt=active.system_prompt)
            await router.flush()

            if stop_reason == "end_turn":
                await adapter.send_completion(
                    external_session_id, "Follow-up completed", session_url=session_url
                )
            else:
                await adapter.send_completion(
                    external_session_id,
                    f"Agent stopped (reason: {stop_reason})",
                    session_url=session_url,
                )
        except Exception:
            logger.exception("Error during follow-up prompt for %s", external_session_id)
            await adapter.send_error(
                external_session_id, "Agent encountered an error during follow-up"
            )
        finally:
            await acp_session.stop()

    async def handle_cancel(self, external_session_id: str) -> None:
        """Cancel an active session."""
        active = self._active_sessions.get(external_session_id)
        if active is None or active.acp_session is None:
            return

        await active.acp_session.cancel()

    def restore_sessions_for_adapter(self, adapter: ServiceAdapter) -> None:
        """Restore persisted sessions for a given adapter.

        This is called when an adapter starts up after a container restart.
        It recreates ActiveSession records from persisted metadata, allowing
        follow-ups to resume with full conversation history.
        """
        if not self._persisted_metadata:
            return

        restored_count = 0
        # Match adapter to sessions: exact match or legacy match (e.g. "slack" matches "slack:default")
        adapter_service_type = adapter.service_name.split(":")[0]
        for external_id, metadata in self._persisted_metadata.items():
            session_service = metadata["service_name"]
            if session_service == adapter.service_name or (
                session_service == adapter_service_type
                and metadata.get("agent_name", "") in ("", adapter.service_name.split(":")[-1])
            ):
                # Skip if already active (shouldn't happen, but be defensive)
                if external_id in self._active_sessions:
                    continue

                # Create a partial ActiveSession that can be resumed on follow-up
                # Use the adapter's service_name so get_sessions_for_service() matches
                self._active_sessions[external_id] = ActiveSession(
                    external_session_id=metadata["external_session_id"],
                    service_name=adapter.service_name,
                    adapter=adapter,
                    acp_session=None,  # Will be created on follow-up
                    update_router=None,  # Will be created on follow-up
                    acp_session_id=metadata["acp_session_id"],
                    cwd=metadata["cwd"],
                    branch_name=metadata.get("branch_name", ""),
                    agent_name=metadata.get("agent_name", ""),
                    service_metadata=metadata.get(
                        "service_metadata"
                    ),  # Restore adapter-specific state
                    github_repo=metadata.get("github_repo", ""),
                    github_installation_id=metadata.get("github_installation_id", 0),
                    system_prompt=metadata.get("system_prompt", ""),
                )
                restored_count += 1

        if restored_count > 0:
            logger.info(
                "Restored %d persisted session(s) for adapter %s",
                restored_count,
                adapter.service_name,
            )

    def get_sessions_for_service(self, service_name: str) -> dict[str, ActiveSession]:
        """Get all active sessions for a given service name."""
        return {
            ext_id: session
            for ext_id, session in self._active_sessions.items()
            if session.service_name == service_name
        }

    async def remove_session(self, external_session_id: str) -> None:
        """Remove a session from tracking, clean up its worktree, and persist."""
        active = self._active_sessions.get(external_session_id)
        if active is not None:
            await self._repo_provider.cleanup_worktree(
                active.cwd, branch_name=active.branch_name, github_repo=active.github_repo
            )
            del self._active_sessions[external_session_id]
            self._save_sessions()
            logger.info("Removed session %s from tracking", external_session_id)

    async def shutdown(self) -> None:
        """Stop all active ACP subprocesses but keep sessions persisted for restart."""
        for active in self._active_sessions.values():
            try:
                if active.acp_session is not None:
                    await active.acp_session.stop()
            except Exception:
                logger.exception("Error stopping session %s", active.external_session_id)
        # Don't clear sessions or save empty state — keep persisted data for restart
        self._active_sessions.clear()
