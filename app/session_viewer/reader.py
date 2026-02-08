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
    # Codex filenames include a prefix: rollout-{timestamp}-{session_id}.jsonl
    if CODEX_SESSIONS_ROOT.exists():
        for jsonl_path in CODEX_SESSIONS_ROOT.rglob(f"*{session_id}.jsonl"):
            return jsonl_path

    return None


def read_session(session_id: str) -> list[dict[str, Any]]:
    """Read and parse a session JSONL file into a list of entries.

    Detects the session format (Claude Code vs Codex) and normalizes Codex
    entries into the Claude Code format the viewer expects.
    Returns an empty list if the session is not found.
    """
    path = find_session_file(session_id)
    if path is None:
        return []

    entries = _read_jsonl(path)
    if not entries:
        return []

    # Detect Codex format: first entry has type "session_meta"
    if entries[0].get("type") == "session_meta":
        return _normalize_codex_entries(entries)

    return entries


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read and parse a JSONL file into raw entries."""
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


def _normalize_codex_entries(raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Codex JSONL entries into the Claude Code viewer format.

    Codex uses:
      - response_item with payload.type "message" (role: user/assistant/developer)
      - response_item with payload.type "reasoning" (thinking summaries)
      - response_item with payload.type "function_call" (tool use)
      - response_item with payload.type "function_call_output" (tool result)

    The viewer expects entries shaped like:
      {type: "user"/"assistant", timestamp, message: {role, content: [blocks]}}
    with content block types: "text", "thinking", "tool_use", "tool_result".
    """
    normalized: list[dict[str, Any]] = []

    for entry in raw_entries:
        if entry.get("type") != "response_item":
            continue

        ts = entry.get("timestamp")
        payload = entry.get("payload", {})
        pt = payload.get("type")

        if pt == "message":
            role = payload.get("role")
            if role == "developer":
                continue  # skip system instructions

            viewer_role = "user" if role == "user" else "assistant"
            content: list[dict[str, Any]] = []
            for block in payload.get("content", []):
                bt = block.get("type")
                if bt in ("input_text", "output_text"):
                    content.append({"type": "text", "text": block.get("text", "")})
                else:
                    content.append(block)
            if not content:
                continue
            normalized.append(
                {
                    "type": viewer_role,
                    "timestamp": ts,
                    "message": {"role": viewer_role, "content": content},
                }
            )

        elif pt == "reasoning":
            # Convert reasoning summaries to a thinking block
            summaries = payload.get("summary", [])
            thinking_text = "\n".join(
                s.get("text", "") for s in summaries if s.get("type") == "summary_text"
            )
            if not thinking_text:
                continue
            normalized.append(
                {
                    "type": "assistant",
                    "timestamp": ts,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": thinking_text}],
                    },
                }
            )

        elif pt == "function_call":
            args_str = payload.get("arguments", "{}")
            try:
                args = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                args = args_str
            normalized.append(
                {
                    "type": "assistant",
                    "timestamp": ts,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": payload.get("call_id"),
                                "name": payload.get("name"),
                                "input": args,
                            }
                        ],
                    },
                }
            )

        elif pt == "function_call_output":
            normalized.append(
                {
                    "type": "user",
                    "timestamp": ts,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": payload.get("call_id"),
                                "content": payload.get("output", ""),
                            }
                        ],
                    },
                }
            )

    return normalized
