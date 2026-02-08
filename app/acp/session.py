"""AcpSession — manages a single agent subprocess + ACP connection."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
from typing import TYPE_CHECKING

from acp import PROTOCOL_VERSION, connect_to_agent, text_block
from acp.schema import ClientCapabilities, Implementation

from app.acp.client import BridgeAcpClient, UpdateCallback

if TYPE_CHECKING:
    from acp.core import ClientSideConnection

logger = logging.getLogger(__name__)


class AcpSession:
    """Manages a single ACP agent subprocess and its connection.

    Lifecycle:
        session = AcpSession(command="claude-code-acp", on_update=callback)
        await session.start(cwd="/path/to/project")
        response = await session.prompt("Fix the bug")
        await session.stop()
    """

    def __init__(
        self,
        command: str = "claude-code-acp",
        on_update: UpdateCallback | None = None,
        env: dict[str, str] | None = None,
    ):
        self._command = command
        self._on_update = on_update
        self._extra_env = env
        self._proc: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]
        self._conn: ClientSideConnection | None = None
        self._client: BridgeAcpClient | None = None
        self._session_id: str | None = None
        self._supports_load_session: bool = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def start(self, cwd: str, resume_session_id: str | None = None) -> str:
        """Spawn the agent process, initialize ACP, and create or resume a session.

        Args:
            cwd: Working directory for the agent
            resume_session_id: If provided, resume this session instead of creating new one

        Returns the ACP session ID.
        """
        proc_env: dict[str, str] | None = None
        if self._extra_env:
            proc_env = {**os.environ, **self._extra_env}

        self._proc = await asyncio.create_subprocess_exec(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=proc_env,
            limit=10 * 1024 * 1024,  # 10MB buffer for large ACP messages
        )

        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("Agent process does not expose stdio pipes")

        self._client = BridgeAcpClient(on_update=self._on_update)
        self._conn = connect_to_agent(self._client, self._proc.stdin, self._proc.stdout)

        init_response = await self._conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(
                name="linear-agent-acp-bridge",
                title="Linear Agent ACP Bridge",
                version="0.1.0",
            ),
        )

        # Check if agent supports session/load (codex-acp) vs session/resume (claude-code-acp)
        caps = init_response.agent_capabilities
        if caps and caps.load_session:
            self._supports_load_session = True

        if resume_session_id:
            # Resume existing session with full conversation history
            if self._supports_load_session:
                # codex-acp uses session/load
                await self._conn.load_session(
                    session_id=resume_session_id,
                    cwd=cwd,
                    mcp_servers=[],
                )
            else:
                # claude-code-acp uses session/resume
                await self._conn.resume_session(
                    session_id=resume_session_id,
                    cwd=cwd,
                    mcp_servers=[],
                )
            self._session_id = resume_session_id
            logger.info("ACP session resumed: %s (cwd=%s)", self._session_id, cwd)
        else:
            # Create new session
            session = await self._conn.new_session(mcp_servers=[], cwd=cwd)
            self._session_id = session.session_id
            logger.info("ACP session started: %s (cwd=%s)", self._session_id, cwd)

        return self._session_id

    async def prompt(self, text: str, system_prompt: str = "") -> str:
        """Send a prompt to the agent and wait for the turn to complete.

        Returns the stop reason.
        """
        if self._conn is None or self._session_id is None:
            raise RuntimeError("Session not started — call start() first")

        response = await self._conn.prompt(
            session_id=self._session_id,
            prompt=[text_block(text)],
            **({"system_prompt": system_prompt} if system_prompt else {}),
        )
        return response.stop_reason

    async def cancel(self) -> None:
        """Cancel the current prompt turn."""
        if self._conn is not None and self._session_id is not None:
            await self._conn.cancel(session_id=self._session_id)

    async def stop(self) -> None:
        """Terminate the agent subprocess and clean up."""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            with contextlib.suppress(ProcessLookupError):
                await self._proc.wait()
            self._proc = None

        self._session_id = None
        logger.info("ACP session stopped")
