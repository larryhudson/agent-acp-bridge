"""RepoProvider — shared repo management and branch creation for all adapters."""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.services.github.auth import GitHubAuth

logger = logging.getLogger(__name__)

# Where the skill source files live (inside the Docker image)
SKILLS_SOURCE_DIR = Path("/app/skills")
# Fallback for local development
SKILLS_SOURCE_DIR_LOCAL = Path(__file__).resolve().parent.parent.parent / "skills"


@dataclass
class RepoSession:
    """Result of preparing a repo for an agent session."""

    cwd: str
    branch_name: str
    env: dict[str, str] = field(default_factory=dict)


def slugify(text: str, max_length: int = 60) -> str:
    """Convert text to a branch-safe slug."""
    # Lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    # Truncate
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "task"


class RepoProvider:
    """Manages a shared git repo, creates branches, and installs skill files.

    Thread-safe via asyncio.Lock — only one session prepares the repo at a time.
    """

    def __init__(
        self,
        auth: GitHubAuth | None = None,
        enabled_services: list[str] | None = None,
    ) -> None:
        self._auth = auth
        self._enabled_services = enabled_services or settings.enabled_services_list
        self._lock = asyncio.Lock()
        self._repo_path = (
            Path("/data/projects") / settings.github_repo if settings.github_repo else None
        )

    async def prepare_new_session(self, descriptive_name: str) -> RepoSession:
        """Clone/fetch the repo and create a new branch for this session.

        Args:
            descriptive_name: Human-readable name for the branch (e.g. issue title).

        Returns:
            RepoSession with cwd, branch_name, and env.
        """
        async with self._lock:
            await self._ensure_repo()

            slug = slugify(descriptive_name)
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            branch_name = f"acp-agent/{slug}-{timestamp}"

            repo_path = self._repo_path
            assert repo_path is not None

            # Create branch from origin/main (or origin/HEAD)
            default_ref = await self._get_default_ref(str(repo_path))
            await self._run_git("checkout", "-B", branch_name, default_ref, cwd=str(repo_path))

            # Install skill files
            self._install_skill_files()

            env = await self.build_agent_env()
            return RepoSession(cwd=str(repo_path), branch_name=branch_name, env=env)

    async def prepare_resume_session(self, branch_name: str) -> RepoSession:
        """Fetch latest and checkout an existing branch for a follow-up session.

        Args:
            branch_name: The branch created by prepare_new_session.

        Returns:
            RepoSession with cwd, branch_name, and refreshed env.
        """
        async with self._lock:
            await self._ensure_repo()

            repo_path = self._repo_path
            assert repo_path is not None

            await self._run_git("checkout", branch_name, cwd=str(repo_path))

            # Re-install skill files (in case they were cleaned or updated)
            self._install_skill_files()

            env = await self.build_agent_env()
            return RepoSession(cwd=str(repo_path), branch_name=branch_name, env=env)

    async def build_agent_env(self) -> dict[str, str]:
        """Build environment variables for the agent subprocess.

        Forwards API keys and generates fresh tokens for enabled services.
        """
        env: dict[str, str] = {}

        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        if settings.openai_api_key:
            env["OPENAI_API_KEY"] = settings.openai_api_key

        # GitHub token (fresh installation token)
        if "github" in self._enabled_services and self._auth is not None:
            try:
                installation_id = settings.github_installation_id
                if installation_id:
                    token = await self._auth.get_installation_token(installation_id)
                    env["GH_TOKEN"] = token
            except Exception:
                logger.exception("Failed to get GitHub installation token for agent env")

        # Slack tokens
        if "slack" in self._enabled_services and settings.slack_bot_token:
            env["SLACK_BOT_TOKEN"] = settings.slack_bot_token
        if "slack" in self._enabled_services and settings.slack_user_token:
            env["SLACK_USER_TOKEN"] = settings.slack_user_token

        # Linear access token
        if "linear" in self._enabled_services and settings.linear_access_token:
            env["LINEAR_ACCESS_TOKEN"] = settings.linear_access_token

        return env

    async def _ensure_repo(self) -> None:
        """Ensure the configured repo is cloned locally, fetching latest if it exists."""
        if not settings.github_repo:
            logger.warning("No github_repo configured — skipping repo setup")
            return

        repo_path = self._repo_path
        assert repo_path is not None

        if repo_path.exists():
            logger.info("Fetching latest for %s", settings.github_repo)
            # Update remote URL with fresh token
            token = await self._get_repo_token()
            if token:
                await self._run_git(
                    "remote",
                    "set-url",
                    "origin",
                    f"https://x-access-token:{token}@github.com/{settings.github_repo}.git",
                    cwd=str(repo_path),
                )
            await self._run_git("fetch", "origin", cwd=str(repo_path))
        else:
            logger.info("Cloning %s into %s", settings.github_repo, repo_path)
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            token = await self._get_repo_token()
            if token:
                clone_url = f"https://x-access-token:{token}@github.com/{settings.github_repo}.git"
            else:
                clone_url = f"https://github.com/{settings.github_repo}.git"
            await self._run_git("clone", clone_url, str(repo_path))

    async def _get_repo_token(self) -> str | None:
        """Get a GitHub token for repo operations."""
        if self._auth is None:
            return None
        installation_id = settings.github_installation_id
        if not installation_id:
            return None
        try:
            return await self._auth.get_installation_token(installation_id)
        except Exception:
            logger.exception("Failed to get installation token for repo operations")
            return None

    async def _get_default_ref(self, cwd: str) -> str:
        """Get the default remote ref (e.g. origin/main)."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--abbrev-ref",
            "origin/HEAD",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return stdout.decode().strip()
        # Fallback
        return "origin/main"

    def _install_skill_files(self) -> None:
        """Copy skill folders for enabled services into global agent skill directories."""
        # Find the skills source directory
        if SKILLS_SOURCE_DIR.exists():
            source = SKILLS_SOURCE_DIR
        elif SKILLS_SOURCE_DIR_LOCAL.exists():
            source = SKILLS_SOURCE_DIR_LOCAL
        else:
            logger.warning("No skills source directory found — skipping skill installation")
            return

        # Target directories (global agent skill dirs, not inside the repo)
        targets = [
            Path("/root/.claude/skills"),
            Path("/root/.codex/skills"),
        ]

        for service in self._enabled_services:
            service_skill_dir = source / service
            if not service_skill_dir.exists():
                continue

            for target_base in targets:
                target_dir = target_base / service
                target_dir.mkdir(parents=True, exist_ok=True)

                # Copy all files from the service skill directory
                for src_file in service_skill_dir.iterdir():
                    if src_file.is_file():
                        shutil.copy2(src_file, target_dir / src_file.name)

        logger.info(
            "Installed skill files for services: %s",
            [s for s in self._enabled_services if (source / s).exists()],
        )

    @staticmethod
    async def _run_git(*args: str, cwd: str | None = None) -> str:
        """Run a git command and return stdout. Raises on non-zero exit."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            cmd_str = " ".join(["git", *args])
            raise RuntimeError(f"git command failed: {cmd_str}\n{stderr.decode()}")
        return stdout.decode()
