"""ACP Client implementation that handles agent callbacks.

This client auto-approves all permission requests and delegates filesystem/terminal
operations to the actual OS, making it suitable for fully autonomous operation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from asyncio.subprocess import Process

from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    EnvVariable,
    KillTerminalCommandResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

logger = logging.getLogger(__name__)

# Union of all session update types the agent can send.
SessionUpdate = (
    UserMessageChunk
    | AgentMessageChunk
    | AgentThoughtChunk
    | ToolCallStart
    | ToolCallProgress
    | AgentPlanUpdate
    | AvailableCommandsUpdate
    | CurrentModeUpdate
)

UpdateCallback = Callable[[str, SessionUpdate], Coroutine[Any, Any, None]]


class _Terminal:
    """Tracks a running subprocess terminal."""

    def __init__(self, proc: Process, terminal_id: str):
        self.proc = proc
        self.terminal_id = terminal_id
        self.output = ""
        self._read_task: asyncio.Task[None] | None = None

    async def start_reading(self) -> None:
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                chunk = await self.proc.stdout.read(4096)
                if not chunk:
                    break
                self.output += chunk.decode("utf-8", errors="replace")
        except Exception:
            pass


class BridgeAcpClient:
    """ACP Client that auto-approves permissions and delegates I/O to the OS.

    The on_update callback is called for every session/update notification from the agent.
    """

    def __init__(self, on_update: UpdateCallback | None = None):
        self._on_update = on_update
        self._terminals: dict[str, _Terminal] = {}

    # --- Permission handling: auto-approve everything ---

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        # Find the first "allow" option, preferring allow_always
        allow_option = None
        for opt in options:
            if opt.kind in ("allow_always", "allow_once"):
                allow_option = opt
                if opt.kind == "allow_always":
                    break

        if allow_option is None:
            # Fallback: pick the first option
            allow_option = options[0]

        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=allow_option.option_id),
        )

    # --- Session updates: forward to callback ---

    async def session_update(
        self,
        session_id: str,
        update: SessionUpdate,
        **kwargs: Any,
    ) -> None:
        if self._on_update:
            try:
                await self._on_update(session_id, update)
            except Exception:
                logger.exception("Error in session update callback")

    # --- Filesystem operations: delegate to OS ---

    async def write_text_file(
        self, content: str, path: str, session_id: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return WriteTextFileResponse()

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        p = Path(path)
        text = p.read_text(encoding="utf-8")

        if line is not None:
            lines = text.splitlines(keepends=True)
            start = max(0, line - 1)  # 1-indexed
            if limit is not None:
                lines = lines[start : start + limit]
            else:
                lines = lines[start:]
            text = "".join(lines)

        return ReadTextFileResponse(content=text)

    # --- Terminal operations: delegate to subprocess ---

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        terminal_id = str(uuid.uuid4())

        proc_env = dict(os.environ)
        if env:
            for var in env:
                proc_env[var.name] = var.value

        proc = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=cwd,
            env=proc_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        terminal = _Terminal(proc, terminal_id)
        await terminal.start_reading()
        self._terminals[terminal_id] = terminal

        return CreateTerminalResponse(terminal_id=terminal_id)

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        terminal = self._terminals[terminal_id]
        return TerminalOutputResponse(
            output=terminal.output,
            exit_status=None,
            truncated=False,
        )

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        terminal = self._terminals.pop(terminal_id, None)
        if terminal and terminal.proc.returncode is None:
            terminal.proc.terminate()
        return ReleaseTerminalResponse()

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        terminal = self._terminals[terminal_id]
        await terminal.proc.wait()
        return WaitForTerminalExitResponse(
            exit_code=terminal.proc.returncode,
        )

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalCommandResponse | None:
        terminal = self._terminals.get(terminal_id)
        if terminal and terminal.proc.returncode is None:
            terminal.proc.kill()
        return KillTerminalCommandResponse()

    # --- Extension methods ---

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        logger.warning("Unhandled ext_method: %s", method)
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.debug("Unhandled ext_notification: %s", method)

    def on_connect(self, conn: Any) -> None:
        pass
