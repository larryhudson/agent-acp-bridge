#!/usr/bin/env python3
"""
PostToolUse Hook for Claude Code
This hook runs after Claude uses a tool that modifies files.
It reads the .post-claude-edit-config.yaml file to determine what checks
to run based on the modified file's path.
"""

import json
import os
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def load_config(project_dir: str) -> dict[str, Any] | None:
    """Load the .post-claude-edit-config.yaml file."""
    config_path = Path(project_dir) / ".post-claude-edit-config.yaml"

    if not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
            return config if config else None
    except Exception as e:
        print(f"Error reading config file: {e}", file=sys.stderr)
        return None


def get_file_path_from_input() -> str | None:
    """Extract file path from stdin JSON."""
    try:
        input_data = json.load(sys.stdin)
        file_path = input_data.get("tool_input", {}).get("file_path")
        return file_path
    except json.JSONDecodeError:
        return None
    except Exception as e:
        print(f"Error parsing input: {e}", file=sys.stderr)
        return None


def matches_patterns(file_path: str, patterns: list[str]) -> bool:
    """Check if file path matches any of the patterns."""
    for pattern in patterns:
        if fnmatch(file_path, pattern) or fnmatch(Path(file_path).name, pattern):
            return True
    return False


def run_command(command: str, file_path: str, project_dir: str) -> tuple[bool, str]:
    """Run a command with file path substitution."""
    # Substitute placeholders
    cmd = command.replace("{file}", file_path)
    cmd = cmd.replace("{dir}", str(Path(file_path).parent))

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=project_dir, capture_output=True, text=True, timeout=30
        )

        output = result.stdout + result.stderr
        success = result.returncode == 0
        return success, output
    except subprocess.TimeoutExpired:
        return False, f"Command timed out: {cmd}"
    except Exception as e:
        return False, f"Error running command: {e}"


def main():
    """Main hook logic."""
    project_dir = os.getenv("CLAUDE_PROJECT_DIR")
    if not project_dir:
        # Fallback to current directory
        project_dir = os.getcwd()

    # Get the file path from input
    file_path = get_file_path_from_input()
    if not file_path:
        # No file to process
        print("{}")
        return

    # Load configuration
    config = load_config(project_dir)
    if not config or "checks" not in config:
        # No configuration found, return empty response
        print("{}")
        return

    # Find matching checks
    checks = config.get("checks", [])
    matching_checks = [
        check
        for check in checks
        if check.get("enabled", True) and matches_patterns(file_path, check.get("patterns", []))
    ]

    if not matching_checks:
        # No matching checks
        print("{}")
        return

    # Run matching checks and collect output
    results = []
    for check in matching_checks:
        command = check.get("command")
        if not command:
            continue

        success, output = run_command(command, file_path, project_dir)
        results.append(
            {
                "name": check.get("name", "unknown"),
                "success": success,
                "output": output.strip() if output else "",
            }
        )

    # Format results as a readable string for additionalContext
    # Only include failures or meaningful output (use exit code to determine)
    context_lines = []
    for result in results:
        output = result["output"]

        # Always include failures (non-zero exit code)
        if not result["success"]:
            context_lines.append(f"[{result['name']}] FAILED")
            if output:
                context_lines.append(output)
            context_lines.append("")
            continue

        # For successful checks, skip if no output
        if not output:
            continue

        # For successful checks with output, only include if there are warnings or fixes
        output_lower = output.lower()
        if any(indicator in output_lower for indicator in ["warning", "error", "fixed", "found"]):
            context_lines.append(f"[{result['name']}]")
            context_lines.append(output)
            context_lines.append("")

    additional_context = "\n".join(context_lines).strip() if context_lines else ""

    # Return results as JSON
    if additional_context:
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": additional_context,
            }
        }
    else:
        response = {}

    print(json.dumps(response))


if __name__ == "__main__":
    main()
