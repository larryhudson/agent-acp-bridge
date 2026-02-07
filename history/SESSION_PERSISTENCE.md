# Session Persistence Implementation

## Overview

This document describes the session persistence implementation that allows the ACP Bridge to survive Docker container restarts without losing active sessions.

## Problem Statement

Previously, active sessions were stored only in memory (`SessionManager._active_sessions`). When the Docker container restarted:
- The in-memory session mapping was lost
- Users could not resume conversations with agents
- Even though ACP session files persisted on disk, we lost the mapping between external session IDs and ACP session IDs

## Solution

The solution involves two components:

### 1. Persistent Session Storage

**File Location**: `/var/lib/bridge/sessions.json`

**Format**:
```json
{
  "sessions": {
    "external_session_id_1": {
      "external_session_id": "external_session_id_1",
      "service_name": "linear",
      "acp_session_id": "acp_session_id_1",
      "cwd": "/data/projects/my-project"
    },
    "external_session_id_2": {
      ...
    }
  }
}
```

**Key Changes to `SessionManager`**:
- `_persistence_file`: Path to the sessions.json file
- `_persisted_metadata`: Cache of persisted session metadata
- `_load_sessions()`: Loads session metadata on startup
- `_save_sessions()`: Saves session metadata to disk (called after creating/updating sessions)
- `restore_sessions_for_adapter()`: Recreates ActiveSession records when adapters start
- `remove_session()`: Removes sessions from tracking and updates the file

### 2. Docker Volume Mounts

**Added to `docker-compose.yml`**:

```yaml
volumes:
  # Session metadata
  - sessions-data:/data
  # Claude Code session files (conversation history, state)
  - claude-sessions:/root/.claude/projects
  # Codex session files (conversation history, state)
  - codex-sessions:/root/.codex/sessions
```

These volumes ensure:
- Session metadata persists across container restarts
- ACP session files (conversation history) persist across restarts
- Follow-ups can resume with full conversation context

## How It Works

### On Initial Session Creation

1. User triggers a new session via Linear/Slack/GitHub
2. `SessionManager.handle_new_session()` creates an ACP session
3. Session metadata is saved to `/var/lib/bridge/sessions.json`
4. Docker volume ensures file persists

### On Container Restart

1. Docker container restarts with volumes intact
2. `SessionManager.__init__()` loads persisted metadata from `/var/lib/bridge/sessions.json`
3. When each adapter starts, `restore_sessions_for_adapter()` is called
4. ActiveSession records are recreated with:
   - `acp_session = None` (will be created on follow-up)
   - `update_router = None` (will be created on follow-up)
   - Original `acp_session_id`, `cwd`, and service metadata

### On Follow-up After Restart

1. User sends a follow-up message
2. `SessionManager.handle_followup()` finds the restored session
3. Creates a new ACP subprocess
4. Calls `acp_session.start(cwd=active.cwd, resume_session_id=active.acp_session_id)`
5. ACP loads full conversation history from `/root/.claude/projects/` or `/root/.codex/sessions/`
6. Agent continues conversation with full context

## File Locations

### Inside Container

- **Session metadata**: `/var/lib/bridge/sessions.json`
- **Claude Code sessions**: `/root/.claude/projects/{project_name}/{session_id}/`
- **Codex sessions**: `/root/.codex/sessions/{session_id}/`

### Docker Volumes

Named volumes managed by Docker:
- `sessions-data`: Stores `/var/lib/bridge/` (contains sessions.json)
- `claude-sessions`: Stores `/root/.claude/projects/`
- `codex-sessions`: Stores `/root/.codex/sessions/`

## Benefits

1. **Seamless Restarts**: Container restarts don't interrupt user workflows
2. **Full Context**: Follow-ups after restart have complete conversation history
3. **Minimal Overhead**: Only lightweight metadata is persisted to JSON
4. **Automatic Recovery**: No manual intervention needed after restarts

## Testing

To test the implementation:

```bash
# Start the bridge
docker-compose up -d

# Create a session via Linear/Slack
# (trigger a Linear issue comment or Slack mention)

# Restart the container
docker-compose restart

# Send a follow-up message
# The agent should resume with full conversation context
```

## Cleanup

Sessions are removed from tracking when:
- `SessionManager.remove_session()` is called explicitly
- `SessionManager.shutdown()` clears all sessions on graceful shutdown

The persistence file is updated atomically using a temporary file to prevent corruption.
