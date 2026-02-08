"""Finds and parses Claude Code / Codex JSONL session files."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Directories where session files are stored (inside Docker container)
CLAUDE_SESSIONS_ROOT = Path("/root/.claude/projects")
CODEX_SESSIONS_ROOT = Path("/root/.codex/sessions")


def find_session_file(session_id: str) -> Path | None:
    """Search known session directories for a JSONL file matching the session ID.

    Claude Code stores sessions as: /root/.claude/projects/{project_dir}/{session_id}.jsonl
    Codex stores sessions in a date hierarchy: /root/.codex/sessions/YYYY/MM/DD/{session_id}.jsonl

    Returns the path to the JSONL file, or None if not found.
    """
    # Search Claude Code sessions
    if CLAUDE_SESSIONS_ROOT.exists():
        for jsonl_path in CLAUDE_SESSIONS_ROOT.rglob(f"{session_id}.jsonl"):
            # Skip subagent files
            if "subagents" not in jsonl_path.parts:
                return jsonl_path

    # Search Codex sessions (use rglob to handle date hierarchy)
    if CODEX_SESSIONS_ROOT.exists():
        for jsonl_path in CODEX_SESSIONS_ROOT.rglob(f"{session_id}.jsonl"):
            return jsonl_path

    return None


def read_session(session_id: str) -> list[dict[str, Any]]:
    """Read and parse a session JSONL file into a list of entries.

    Filters to only conversation-relevant entries (user/assistant messages).
    Returns an empty list if the session is not found.
    """
    path = find_session_file(session_id)
    if path is None:
        return []

    return read_session_file(path)


def read_session_file(path: Path) -> list[dict[str, Any]]:
    """Read and parse a JSONL file into conversation entries."""
    entries: list[dict[str, Any]] = []

    try:
        with open(path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries.append(entry)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSON on line %d of %s", line_num, path)
    except Exception:
        logger.exception("Failed to read session file: %s", path)

    return entries
