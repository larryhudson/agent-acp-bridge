#!/bin/bash
#
# PostToolUse Hook: LSP Diagnostics Check
#
# Queries the LSP bridge daemon for TypeScript/Python diagnostics after a
# file is written or edited. Returns errors/warnings as additionalContext
# so Claude sees them immediately.
#
# Input (stdin): JSON with tool_input.file_path
# Output (stdout): JSON with hookSpecificOutput.additionalContext (if errors)

set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
SOCKET_FILE="$PROJECT_DIR/.claude/hooks/lsp-bridge.socket"

# Read the hook input from stdin
INPUT=$(cat)

# Extract the file path
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [ -z "$FILE_PATH" ]; then
  echo '{}'
  exit 0
fi

# Check if the LSP bridge is running
if [ ! -f "$SOCKET_FILE" ]; then
  # Bridge not running — skip silently
  echo '{}'
  exit 0
fi

SOCKET_PATH=$(cat "$SOCKET_FILE")

if [ ! -S "$SOCKET_PATH" ]; then
  # Socket doesn't exist — bridge probably died
  echo '{}'
  exit 0
fi

# Query the bridge for diagnostics
RESPONSE=$(curl -s --unix-socket "$SOCKET_PATH" \
  -X POST \
  -H "Content-Type: application/json" \
  -d "{\"file\": \"$FILE_PATH\"}" \
  "http://localhost/diagnostics" 2>/dev/null) || {
  # curl failed — bridge may be down
  echo '{}'
  exit 0
}

# Check if we got a valid response with diagnostics
DIAG_COUNT=$(echo "$RESPONSE" | jq '.diagnostics | length' 2>/dev/null) || {
  echo '{}'
  exit 0
}

if [ "$DIAG_COUNT" -eq 0 ]; then
  echo '{}'
  exit 0
fi

# Make path relative to project dir for compact output
REL_PATH="${FILE_PATH#$PROJECT_DIR/}"

# Format diagnostics compactly — minimize tokens for LLM context
CONTEXT=$(echo "$RESPONSE" | jq -r --arg rel "$REL_PATH" '
  (.diagnostics | map(select(.severity == "error")) | length) as $e |
  (.diagnostics | map(select(.severity == "warning")) | length) as $w |
  (
    (if $e > 0 then "\($e) error\(if $e > 1 then "s" else "" end)" else "" end) +
    (if $e > 0 and $w > 0 then ", " else "" end) +
    (if $w > 0 then "\($w) warning\(if $w > 1 then "s" else "" end)" else "" end) +
    (if $e == 0 and $w == 0 then "\(.diagnostics | length) issues" else "" end)
  ) as $summary |
  "LSP \($rel) (\($summary))",
  (.diagnostics | sort_by(.range.start.line)[] |
    "  \(.range.start.line): \(.message)")
')

# Build the hook response with additionalContext
jq -n --arg ctx "$CONTEXT" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: $ctx
  }
}'
