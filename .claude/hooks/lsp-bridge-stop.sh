#!/bin/bash
#
# SessionEnd Hook: Stop the LSP bridge daemon
#
# Sends a shutdown request to the bridge and cleans up PID/socket files.

set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
PID_FILE="$PROJECT_DIR/.claude/hooks/lsp-bridge.pid"
SOCKET_FILE="$PROJECT_DIR/.claude/hooks/lsp-bridge.socket"

# Try graceful shutdown via API first
if [ -f "$SOCKET_FILE" ]; then
  SOCKET_PATH=$(cat "$SOCKET_FILE")
  if [ -S "$SOCKET_PATH" ]; then
    curl -s --unix-socket "$SOCKET_PATH" \
      -X POST "http://localhost/shutdown" > /dev/null 2>&1 || true
    # Give it a moment to clean up
    sleep 1
  fi
fi

# If still running, kill by PID
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    # Wait briefly, then force kill if needed
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE"
fi

# Clean up socket files
if [ -f "$SOCKET_FILE" ]; then
  SOCKET_PATH=$(cat "$SOCKET_FILE")
  rm -f "$SOCKET_PATH" 2>/dev/null || true
  rm -f "$SOCKET_FILE"
fi

echo '{}'
