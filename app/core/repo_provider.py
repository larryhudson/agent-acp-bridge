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
    """Manages git repos, creates worktrees, and installs skill files.

    Each agent session gets its own git worktree so multiple sessions can run
    concurrently without working-tree conflicts.

    Supports per-session repo overrides: each session can specify a different
    GitHub repo and installation ID. Falls back to the global GITHUB_REPO and
    GITHUB_INSTALLATION_ID settings when not specified.

    Thread-safe via asyncio.Lock — only one session prepares a repo at a time.
    """

    def __init__(
        self,
        auth: GitHubAuth | None = None,
        auth_map: dict[str, GitHubAuth] | None = None,
        enabled_services: list[str] | None = None,
    ) -> None:
        self._auth = auth  # Default auth for repo operations (clone/fetch)
        self._auth_map = auth_map or {}  # Per-agent auth for GH_TOKEN generation
        self._enabled_services = enabled_services or settings.enabled_services_list
        self._lock = asyncio.Lock()
        self._worktrees_base = Path("/data/worktrees")

    def _repo_path(self, github_repo: str) -> Path:
        """Get the local path for a given repo."""
        return Path("/data/projects") / github_repo

    def _resolve_repo(self, github_repo: str = "") -> str:
        """Resolve the repo to use: per-session override or global default."""
        return github_repo or settings.github_repo

    def _resolve_installation_id(self, github_installation_id: int = 0) -> int:
        """Resolve the installation ID: per-session override or global default."""
        return github_installation_id or settings.github_installation_id

    async def prepare_new_session(
        self,
        descriptive_name: str,
        agent_name: str = "",
        github_repo: str = "",
        github_installation_id: int = 0,
    ) -> RepoSession:
        """Clone/fetch the repo and create a new worktree + branch for this session.

        Each session gets its own worktree directory so multiple agents can work
        concurrently without interfering with each other's working trees.

        Args:
            descriptive_name: Human-readable name for the branch (e.g. issue title).
            agent_name: Which agent this session is for (for per-agent GH_TOKEN).
            github_repo: Override repo (e.g. "owner/repo"). Falls back to default.
            github_installation_id: Override installation ID. Falls back to default.

        Returns:
            RepoSession with cwd (worktree path), branch_name, and env.
        """
        repo = self._resolve_repo(github_repo)
        installation_id = self._resolve_installation_id(github_installation_id)

        async with self._lock:
            await self._ensure_repo(repo, installation_id)

            slug = slugify(descriptive_name)
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            branch_name = f"acp-agent/{slug}-{timestamp}"

            repo_path = self._repo_path(repo)

            # Create an isolated worktree for this session
            worktree_dir = self._worktree_path_for(slug, timestamp, github_repo=repo)
            worktree_dir.parent.mkdir(parents=True, exist_ok=True)

            default_ref = await self._get_default_ref(str(repo_path))
            await self._run_git(
                "worktree",
                "add",
                "-b",
                branch_name,
                str(worktree_dir),
                default_ref,
                cwd=str(repo_path),
            )

            # Install skill files
            self._install_skill_files()

            env = await self.build_agent_env(
                agent_name=agent_name, installation_id=installation_id
            )
            return RepoSession(cwd=str(worktree_dir), branch_name=branch_name, env=env)

    async def prepare_resume_session(
        self,
        branch_name: str,
        cwd: str | None = None,
        agent_name: str = "",
        github_repo: str = "",
        github_installation_id: int = 0,
    ) -> RepoSession:
        """Fetch latest and prepare an existing worktree for a follow-up session.

        If ``cwd`` points to an existing worktree directory it is reused directly.
        Otherwise falls back to checking out the branch in the main repo (backward
        compatibility for sessions created before worktree support).

        Args:
            branch_name: The branch created by prepare_new_session.
            cwd: The worktree path stored from the original session (if available).
            agent_name: Which agent this session is for (for per-agent GH_TOKEN).
            github_repo: Override repo (e.g. "owner/repo"). Falls back to default.
            github_installation_id: Override installation ID. Falls back to default.

        Returns:
            RepoSession with cwd, branch_name, and refreshed env.
        """
        repo = self._resolve_repo(github_repo)
        installation_id = self._resolve_installation_id(github_installation_id)

        async with self._lock:
            await self._ensure_repo(repo, installation_id)

            repo_path = self._repo_path(repo)

            if cwd and Path(cwd).exists() and Path(cwd).resolve() != repo_path.resolve():
                # Worktree already exists — just fetch to update remote refs
                worktree_path = cwd
            else:
                # Fallback for pre-worktree sessions: checkout in the main repo
                await self._run_git("checkout", branch_name, cwd=str(repo_path))
                worktree_path = str(repo_path)

            # Re-install skill files (in case they were cleaned or updated)
            self._install_skill_files()

            env = await self.build_agent_env(
                agent_name=agent_name, installation_id=installation_id
            )
            return RepoSession(cwd=worktree_path, branch_name=branch_name, env=env)

    async def cleanup_worktree(
        self, cwd: str, branch_name: str = "", github_repo: str = ""
    ) -> None:
        """Remove a git worktree, its branch, and clean up its directory.

        Safe to call with the main repo path — it will be skipped.
        """
        repo = self._resolve_repo(github_repo)
        if not repo:
            return

        repo_path = self._repo_path(repo)
        worktree_path = Path(cwd)
        if not worktree_path.exists():
            return

        # Never remove the main repo itself
        if worktree_path.resolve() == repo_path.resolve():
            return

        async with self._lock:
            try:
                await self._run_git(
                    "worktree", "remove", "--force", str(worktree_path), cwd=str(repo_path)
                )
                logger.info("Removed worktree at %s", worktree_path)
            except RuntimeError:
                logger.warning(
                    "Failed to remove worktree via git at %s, cleaning up manually",
                    worktree_path,
                )
                shutil.rmtree(worktree_path, ignore_errors=True)

            # Prune stale worktree entries
            try:
                await self._run_git("worktree", "prune", cwd=str(repo_path))
            except RuntimeError:
                pass

            # Delete the orphaned branch to avoid accumulating stale refs
            if branch_name:
                try:
                    await self._run_git("branch", "-D", branch_name, cwd=str(repo_path))
                    logger.info("Deleted branch %s", branch_name)
                except RuntimeError:
                    logger.debug(
                        "Could not delete branch %s (may already be gone)", branch_name
                    )

    async def build_agent_env(
        self, agent_name: str = "", installation_id: int = 0
    ) -> dict[str, str]:
        """Build environment variables for the agent subprocess.

        Forwards API keys and generates fresh tokens for enabled services.
        Uses agent-specific GitHub auth/installation when available.

        Args:
            agent_name: Which agent to build env for (for per-agent auth).
            installation_id: GitHub installation ID for token generation.
                Falls back to agent-specific or global default if 0.
        """
        env: dict[str, str] = {}

        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        if settings.openai_api_key:
            env["OPENAI_API_KEY"] = settings.openai_api_key

        # GitHub token — use agent-specific auth + per-session installation ID
        if "github" in self._enabled_services:
            auth = self._auth_map.get(agent_name, self._auth) if agent_name else self._auth
            if auth is not None:
                try:
                    # Per-session installation_id overrides agent-specific and global defaults
                    effective_id = installation_id
                    if not effective_id and agent_name:
                        id_str = settings.get_service_credential(
                            "GITHUB_INSTALLATION_ID", agent_name
                        )
                        if id_str:
                            effective_id = int(id_str)
                    if not effective_id:
                        effective_id = settings.github_installation_id
                    if effective_id:
                        token = await auth.get_installation_token(effective_id)
                        env["GH_TOKEN"] = token
                except Exception:
                    logger.exception(
                        "Failed to get GitHub installation token for agent env (agent=%s)",
                        agent_name,
                    )

        # Slack tokens
        if "slack" in self._enabled_services and settings.slack_bot_token:
            env["SLACK_BOT_TOKEN"] = settings.slack_bot_token
        if "slack" in self._enabled_services and settings.slack_user_token:
            env["SLACK_USER_TOKEN"] = settings.slack_user_token

        # Linear access token
        if "linear" in self._enabled_services and settings.linear_access_token:
            env["LINEAR_ACCESS_TOKEN"] = settings.linear_access_token

        return env

    def _worktree_path_for(
        self, slug: str, timestamp: str, github_repo: str = ""
    ) -> Path:
        """Return the filesystem path for a session worktree."""
        repo = self._resolve_repo(github_repo)
        assert repo
        return self._worktrees_base / repo / f"{slug}-{timestamp}"

    async def _ensure_repo(self, github_repo: str, installation_id: int) -> None:
        """Ensure the given repo is cloned locally, fetching latest if it exists."""
        if not github_repo:
            logger.warning("No github_repo specified — skipping repo setup")
            return

        if github_repo.count("/") != 1:
            raise ValueError(f"Invalid repo format {github_repo!r} — expected 'owner/repo'")

        repo_path = self._repo_path(github_repo)

        if repo_path.exists():
            logger.info("Fetching latest for %s", github_repo)
            # Update remote URL with fresh token
            token = await self._get_repo_token(github_repo, installation_id)
            if token:
                await self._run_git(
                    "remote",
                    "set-url",
                    "origin",
                    f"https://x-access-token:{token}@github.com/{github_repo}.git",
                    cwd=str(repo_path),
                )
            await self._run_git("fetch", "origin", cwd=str(repo_path))
        else:
            logger.info("Cloning %s into %s", github_repo, repo_path)
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            token = await self._get_repo_token(github_repo, installation_id)
            if token:
                clone_url = (
                    f"https://x-access-token:{token}@github.com/{github_repo}.git"
                )
            else:
                clone_url = f"https://github.com/{github_repo}.git"
            await self._run_git("clone", clone_url, str(repo_path))

    async def _get_repo_token(
        self, github_repo: str, installation_id: int
    ) -> str | None:
        """Get a GitHub token for repo operations."""
        if self._auth is None:
            return None
        resolved_id = self._resolve_installation_id(installation_id)
        if not resolved_id:
            return None
        try:
            return await self._auth.get_installation_token(resolved_id)
        except Exception:
            logger.exception(
                "Failed to get installation token for repo %s", github_repo
            )
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
            logger.warning(
                "No skills source directory found — skipping skill installation"
            )
            return

        # Target directories (global agent skill dirs, not inside the repo)
        home = Path.home()
        targets = [
            home / ".claude" / "skills",
            home / ".codex" / "skills",
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
