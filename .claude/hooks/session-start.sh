#!/bin/bash

# SessionStart Hook for Claude Code
# This hook runs when a Claude Code session starts
# It ensures dev dependencies are set up and provides development context

cd "$CLAUDE_PROJECT_DIR" || exit 1

# Function to install a package using appropriate package manager
install_package() {
  local package=$1
  echo "Installing $package..."

  if command -v apt-get &> /dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y -qq "$package"
  elif command -v yum &> /dev/null; then
    sudo yum install -y -q "$package"
  elif command -v brew &> /dev/null; then
    brew install "$package"
  else
    echo "Warning: No supported package manager found (apt-get, yum, or brew)"
    return 1
  fi
}

# Install jq if missing
if ! command -v jq &> /dev/null; then
  echo "jq not found, installing..."
  install_package jq
fi

# Install npm/nodejs if missing
if ! command -v npm &> /dev/null; then
  echo "npm not found, installing nodejs..."
  if command -v apt-get &> /dev/null; then
    install_package nodejs npm
  else
    install_package nodejs
  fi
fi

# Check for hivemind but don't auto-install (manual installation recommended)
if ! command -v hivemind &> /dev/null; then
  echo "Note: hivemind is not installed."
  echo "      Install manually: https://github.com/DarthSim/hivemind#installation"
  echo "      You can still use the project, but 'make dev' will not work."
fi

# Ensure npm dependencies are installed if package.json exists
if [ -f "package.json" ] && [ ! -d "node_modules" ]; then
  echo "Installing npm dependencies..."
  npm install
fi

# Ensure frontend npm dependencies are installed
if [ -f "frontend/package.json" ] && [ ! -d "frontend/node_modules" ]; then
  echo "Installing frontend npm dependencies..."
  (cd frontend && npm install)
fi

# Ensure Python dependencies are synced if pyproject.toml exists
if [ -f "pyproject.toml" ] && command -v uv &> /dev/null; then
  echo "Syncing Python dependencies with uv..."
  uv sync --quiet
fi

# Set up pre-commit hooks if .pre-commit-config.yaml exists
if [ -f ".pre-commit-config.yaml" ]; then
  # Install pre-commit if not available
  if ! command -v pre-commit &> /dev/null; then
    if command -v uv &> /dev/null; then
      echo "Installing pre-commit with uv..."
      uv tool install pre-commit --quiet
    else
      echo "Warning: pre-commit not installed and uv not available"
    fi
  fi

  # Install git hooks if pre-commit is available
  if command -v pre-commit &> /dev/null; then
    # Check if hooks are already installed by looking for pre-commit hook
    if [ ! -f ".git/hooks/pre-commit" ] || ! grep -q "pre-commit" ".git/hooks/pre-commit" 2>/dev/null; then
      echo "Installing pre-commit git hooks..."
      pre-commit install
    fi
  fi
fi

# Start the LSP bridge daemon — reads server config from lsp-servers.yaml
start_lsp_bridge() {
  local PID_FILE="$CLAUDE_PROJECT_DIR/.claude/hooks/lsp-bridge.pid"
  local BRIDGE_SCRIPT="$CLAUDE_PROJECT_DIR/.claude/hooks/lsp-bridge.mjs"
  local CONFIG_FILE="$CLAUDE_PROJECT_DIR/.claude/hooks/lsp-servers.yaml"

  # Check if bridge is already running
  if [ -f "$PID_FILE" ]; then
    local OLD_PID
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "LSP bridge already running (PID $OLD_PID)"
      return 0
    fi
    # Stale PID file — clean up
    rm -f "$PID_FILE"
  fi

  # Need both the config file and the bridge script
  if [ ! -f "$CONFIG_FILE" ]; then
    echo "No LSP server config found ($CONFIG_FILE), skipping LSP bridge"
    return 0
  fi

  if [ ! -f "$BRIDGE_SCRIPT" ]; then
    echo "LSP bridge script not found ($BRIDGE_SCRIPT), skipping"
    return 0
  fi

  echo "Starting LSP bridge daemon..."
  nohup node "$BRIDGE_SCRIPT" > /dev/null 2>&1 &

  # Wait briefly for the bridge to initialize
  sleep 3

  if [ -f "$PID_FILE" ]; then
    local BRIDGE_PID
    BRIDGE_PID=$(cat "$PID_FILE")
    echo "LSP bridge started (PID $BRIDGE_PID) — config: $CONFIG_FILE"
  else
    echo "Warning: LSP bridge may have failed to start. Check .claude/hooks/lsp-bridge.log"
  fi
}

# Start bridge if config exists
if [ -f ".claude/hooks/lsp-servers.yaml" ]; then
  start_lsp_bridge
fi

# Provide development context
echo "Development environment loaded for: $CLAUDE_PROJECT_DIR"
echo "Available commands:"
echo "  - make dev       : Start development server"
echo "  - make dev-logs  : View development logs"
echo "  - make lint-file : Lint and format a specific file"
