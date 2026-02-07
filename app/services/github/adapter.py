"""GitHubAdapter — implements ServiceAdapter for GitHub App webhooks."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import FastAPI, Request, Response

from app.config import settings
from app.core.repo_provider import slugify
from app.core.types import BridgeSessionRequest, BridgeUpdate
from app.services.github.api_client import GitHubApiClient
from app.services.github.auth import GitHubAuth
from app.services.github.models import (
    IssueCommentPayload,
    IssuesPayload,
    PullRequestReviewCommentPayload,
)
from app.services.github.webhooks import verify_signature

logger = logging.getLogger(__name__)


class GitHubAdapter:
    """Service adapter for GitHub App webhooks.

    Handles:
    - Webhook reception and signature verification
    - Issue comment and PR review comment @mentions
    - Edit-in-place progress updates (like Slack's pattern)
    - Session lifecycle management
    """

    service_name: str = "github"

    def __init__(self, session_manager: Any, auth: GitHubAuth | None = None) -> None:
        self._session_manager = session_manager
        self._auth = auth or GitHubAuth()
        self._api = GitHubApiClient(self._auth)
        self._bot_login: str | None = settings.github_bot_login or None
        # Track session data: {session_id: {owner, repo, issue_number, ...}}
        self._sessions: dict[str, dict[str, Any]] = {}
        # Accumulate message chunks for final response
        self._message_buffers: dict[str, str] = {}
        # Track issues/PRs where bot has been mentioned (persists after session completion)
        # Format: {session_id} — e.g. "github:owner/repo:42"
        self._active_issues: set[str] = set()
        # Keep references to background tasks so they aren't garbage collected
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Auto-detect the bot login from the GitHub App at startup."""
        if not self._bot_login:
            try:
                slug = await self._auth.get_app_slug()
                self._bot_login = f"{slug}[bot]"
                logger.info("Auto-detected GitHub bot login: %s", self._bot_login)
            except Exception:
                logger.exception(
                    "Failed to auto-detect GitHub App slug; @mention detection may fail"
                )

    def restore_persisted_sessions(self) -> None:
        """Rebuild adapter state from sessions restored by the session manager."""
        restored = self._session_manager.get_sessions_for_service(self.service_name)
        for session_id, active in restored.items():
            if active.service_metadata:
                self._sessions[session_id] = active.service_metadata
                self._active_issues.add(session_id)
        if restored:
            logger.info("Restored %d GitHub session(s) from persistence", len(restored))

    def register_routes(self, app: FastAPI) -> None:
        """Register the GitHub webhook endpoint."""

        @app.post("/webhooks/github")
        async def handle_github_webhook(request: Request) -> Response:
            raw_body = await request.body()

            # Verify signature
            signature = request.headers.get("X-Hub-Signature-256", "")
            if settings.github_webhook_secret and not verify_signature(
                raw_body, signature, settings.github_webhook_secret
            ):
                logger.warning("Invalid GitHub webhook signature")
                return Response(status_code=400)

            event_type = request.headers.get("X-GitHub-Event", "")

            # Handle ping event (sent when webhook is first configured)
            if event_type == "ping":
                logger.info("GitHub webhook ping received")
                return Response(status_code=200)

            if event_type == "issues":
                issues_payload = IssuesPayload.model_validate_json(raw_body)
                if issues_payload.action == "opened":
                    task = asyncio.create_task(self._handle_issue_opened(issues_payload))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)

            elif event_type == "issue_comment":
                payload = IssueCommentPayload.model_validate_json(raw_body)
                if payload.action == "created":
                    task = asyncio.create_task(self._handle_issue_comment(payload))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)

            elif event_type == "pull_request_review_comment":
                payload = PullRequestReviewCommentPayload.model_validate_json(raw_body)
                if payload.action == "created":
                    task = asyncio.create_task(self._handle_review_comment(payload))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)

            else:
                logger.debug("Ignoring GitHub event: %s", event_type)

            return Response(status_code=200)

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        """Not used — webhooks handle this via background tasks."""
        raise NotImplementedError

    def _is_bot_comment(self, user_type: str, user_login: str) -> bool:
        """Check if a comment was posted by our bot."""
        if user_type == "Bot":
            # If we know the bot login, match it exactly
            if self._bot_login and user_login == self._bot_login:
                return True
            # If we don't know the login yet, ignore all bot comments to be safe
            if not self._bot_login:
                return True
        return False

    def _extract_mention(self, body: str) -> str | None:
        """Extract the prompt text if the comment mentions our bot.

        Returns the comment body with the @mention stripped, or None if not mentioned.
        """
        if not self._bot_login:
            return None

        # The bot login is "slug[bot]" — users @mention as @slug or @slug[bot]
        # Extract the slug part (before [bot])
        slug = self._bot_login.removesuffix("[bot]")

        # Match @slug or @slug[bot] (case-insensitive)
        pattern = re.compile(
            rf"@{re.escape(slug)}(?:\[bot\])?\s*",
            re.IGNORECASE,
        )

        if not pattern.search(body):
            return None

        prompt = pattern.sub("", body).strip()
        return prompt

    async def _handle_issue_opened(self, payload: IssuesPayload) -> None:
        """Handle an issues event with action 'opened'."""
        issue = payload.issue
        repo = payload.repository

        # Ignore issues created by our bot
        if self._is_bot_comment(payload.sender.type, payload.sender.login):
            return

        # Check for @mention in the issue body
        if not issue.body:
            return
        prompt = self._extract_mention(issue.body)
        if prompt is None:
            return

        if not prompt:
            logger.info("Empty prompt after stripping @mention in issue body, ignoring")
            return

        installation_id = payload.installation.id if payload.installation else None
        if not installation_id:
            logger.warning("No installation ID in webhook payload")
            return

        owner, repo_name = repo.full_name.split("/", 1)
        issue_number = issue.number
        session_id = f"github:{repo.full_name}:{issue_number}"

        logger.info(
            "GitHub @mention in new issue body: session=%s, user=%s",
            session_id,
            payload.sender.login,
        )

        try:
            # Acknowledge with eyes reaction on the issue
            await self._api.create_issue_reaction(
                owner, repo_name, issue_number, "eyes", installation_id
            )

            # Post progress comment
            progress = await self._api.create_comment(
                owner, repo_name, issue_number, "_Thinking..._", installation_id
            )
            progress_comment_id = progress["id"]
        except Exception:
            logger.exception("Failed to post initial progress comment for %s", session_id)
            return

        # Build prompt with issue context
        context_parts = [
            f"GitHub issue: {issue.title} (#{issue.number})",
            f"Issue body:\n{prompt}",
        ]
        full_prompt = "\n\n".join(context_parts)

        # Track session
        session_data = {
            "owner": owner,
            "repo": repo_name,
            "issue_number": issue_number,
            "installation_id": installation_id,
            "trigger_comment_id": None,
            "trigger_issue_number": issue_number,
            "progress_comment_id": progress_comment_id,
            "current_text": "_Thinking..._",
            "is_review_comment": False,
        }
        self._sessions[session_id] = session_data

        # Mark this issue as active (persists after session ends)
        self._active_issues.add(session_id)

        request = BridgeSessionRequest(
            external_session_id=session_id,
            service_name=self.service_name,
            prompt=full_prompt,
            descriptive_name=slugify(issue.title),
            service_metadata=session_data,
        )

        await self._session_manager.handle_new_session(self, request)

    async def _handle_issue_comment(self, payload: IssueCommentPayload) -> None:
        """Handle an issue_comment event."""
        comment = payload.comment
        repo = payload.repository

        # Ignore bot's own comments
        if self._is_bot_comment(comment.user.type, comment.user.login):
            return

        # Check for @mention
        prompt = self._extract_mention(comment.body)
        if prompt is None:
            return

        if not prompt:
            logger.info("Empty prompt after stripping @mention, ignoring")
            return

        installation_id = payload.installation.id if payload.installation else None
        if not installation_id:
            logger.warning("No installation ID in webhook payload")
            return

        owner, repo_name = repo.full_name.split("/", 1)
        issue_number = payload.issue.number
        session_id = f"github:{repo.full_name}:{issue_number}"

        logger.info(
            "GitHub @mention in issue comment: session=%s, user=%s",
            session_id,
            comment.user.login,
        )

        try:
            # Acknowledge with eyes reaction
            await self._api.create_reaction(owner, repo_name, comment.id, "eyes", installation_id)

            # Post progress comment
            progress = await self._api.create_comment(
                owner, repo_name, issue_number, "_Thinking..._", installation_id
            )
            progress_comment_id = progress["id"]
        except Exception:
            logger.exception("Failed to post initial progress comment for %s", session_id)
            return

        # Check if this is a follow-up on an active issue
        if session_id in self._active_issues:
            if session_id in self._sessions:
                # Active session — send follow-up
                logger.info("Sending follow-up to existing session %s", session_id)
                # Update session tracking with new progress comment
                self._sessions[session_id]["trigger_comment_id"] = comment.id
                self._sessions[session_id]["progress_comment_id"] = progress_comment_id
                self._sessions[session_id]["current_text"] = "_Thinking..._"
                await self._session_manager.handle_followup(session_id, prompt)
                return
            else:
                # Session completed but issue still active — start new session
                logger.info("Creating new session for continued issue conversation %s", session_id)

        # Build prompt with issue context
        issue = payload.issue
        context_parts = [
            f"GitHub issue: {issue.title} (#{issue.number})",
        ]
        if issue.body:
            context_parts.append(f"Issue body:\n{issue.body}")
        context_parts.append(f"User @{comment.user.login} commented:\n{prompt}")
        full_prompt = "\n\n".join(context_parts)

        # Track session
        session_data = {
            "owner": owner,
            "repo": repo_name,
            "issue_number": issue_number,
            "installation_id": installation_id,
            "trigger_comment_id": comment.id,
            "progress_comment_id": progress_comment_id,
            "current_text": "_Thinking..._",
            "is_review_comment": False,
        }
        self._sessions[session_id] = session_data

        # Mark this issue as active (persists after session ends)
        self._active_issues.add(session_id)

        request = BridgeSessionRequest(
            external_session_id=session_id,
            service_name=self.service_name,
            prompt=full_prompt,
            descriptive_name=slugify(issue.title),
            service_metadata=session_data,
        )

        await self._session_manager.handle_new_session(self, request)

    async def _handle_review_comment(self, payload: PullRequestReviewCommentPayload) -> None:
        """Handle a pull_request_review_comment event."""
        comment = payload.comment
        repo = payload.repository

        # Ignore bot's own comments
        if self._is_bot_comment(comment.user.type, comment.user.login):
            return

        # Check for @mention
        prompt = self._extract_mention(comment.body)
        if prompt is None:
            return

        if not prompt:
            logger.info("Empty prompt after stripping @mention in review comment, ignoring")
            return

        installation_id = payload.installation.id if payload.installation else None
        if not installation_id:
            logger.warning("No installation ID in webhook payload")
            return

        owner, repo_name = repo.full_name.split("/", 1)
        pr_number = payload.pull_request.number
        session_id = f"github:{repo.full_name}:{pr_number}"

        logger.info(
            "GitHub @mention in PR review comment: session=%s, user=%s",
            session_id,
            comment.user.login,
        )

        try:
            # Acknowledge with eyes reaction
            await self._api.create_reaction(
                owner,
                repo_name,
                comment.id,
                "eyes",
                installation_id,
                is_review_comment=True,
            )

            # Reply in the review comment thread
            progress = await self._api.create_review_comment_reply(
                owner, repo_name, pr_number, comment.id, "_Thinking..._", installation_id
            )
            progress_comment_id = progress["id"]
        except Exception:
            logger.exception("Failed to post initial review comment reply for %s", session_id)
            return

        # Check if this is a follow-up on an active PR
        if session_id in self._active_issues:
            if session_id in self._sessions:
                # Active session — send follow-up
                logger.info("Sending follow-up to existing session %s", session_id)
                self._sessions[session_id]["trigger_comment_id"] = comment.id
                self._sessions[session_id]["progress_comment_id"] = progress_comment_id
                self._sessions[session_id]["current_text"] = "_Thinking..._"
                self._sessions[session_id]["is_review_comment"] = True
                await self._session_manager.handle_followup(session_id, prompt)
                return
            else:
                logger.info("Creating new session for continued PR conversation %s", session_id)

        # Build prompt with PR + diff context
        pr = payload.pull_request
        context_parts = [
            f"Pull request: {pr.title} (#{pr.number})",
        ]
        if pr.body:
            context_parts.append(f"PR description:\n{pr.body}")
        if comment.path:
            context_parts.append(f"File: {comment.path}")
        if comment.diff_hunk:
            context_parts.append(f"Diff context:\n```\n{comment.diff_hunk}\n```")
        if comment.line:
            context_parts.append(f"Line: {comment.line}")
        context_parts.append(f"User @{comment.user.login} commented:\n{prompt}")
        full_prompt = "\n\n".join(context_parts)

        # Track session
        session_data = {
            "owner": owner,
            "repo": repo_name,
            "issue_number": pr_number,
            "installation_id": installation_id,
            "trigger_comment_id": comment.id,
            "progress_comment_id": progress_comment_id,
            "current_text": "_Thinking..._",
            "is_review_comment": True,
        }
        self._sessions[session_id] = session_data

        # Mark this PR as active (persists after session ends)
        self._active_issues.add(session_id)

        request = BridgeSessionRequest(
            external_session_id=session_id,
            service_name=self.service_name,
            prompt=full_prompt,
            descriptive_name=slugify(pr.title),
            service_metadata=session_data,
        )

        await self._session_manager.handle_new_session(self, request)

    async def send_update(self, session_id: str, update: BridgeUpdate) -> None:
        """Translate BridgeUpdate to GitHub comment edits."""
        session_data = self._sessions.get(session_id)
        if not session_data:
            logger.warning("No session data for %s, cannot send update", session_id)
            return

        try:
            owner = session_data["owner"]
            repo = session_data["repo"]
            installation_id = session_data["installation_id"]
            progress_id = session_data["progress_comment_id"]
            is_review = session_data["is_review_comment"]

            if update.type == "thought":
                new_text = f"_Thinking: {update.content}_"
                await self._update_progress(
                    owner, repo, progress_id, new_text, installation_id, is_review
                )
                session_data["current_text"] = new_text

            elif update.type == "tool_call":
                current = session_data.get("current_text", "")
                tool_name = update.content
                locations = update.metadata.get("locations", [])
                tool_line = f"\n- `{tool_name}`"
                if locations:
                    tool_line += f" ({', '.join(locations)})"
                new_text = current + tool_line
                await self._update_progress(
                    owner, repo, progress_id, new_text, installation_id, is_review
                )
                session_data["current_text"] = new_text

            elif update.type == "message_chunk":
                self._message_buffers.setdefault(session_id, "")
                self._message_buffers[session_id] += update.content

            elif update.type == "plan":
                entries = update.metadata.get("entries", [])
                plan_lines = ["**Plan:**"]
                for entry in entries:
                    status = entry.get("status", "pending")
                    checkbox = "[x]" if status == "completed" else "[ ]"
                    plan_lines.append(f"- {checkbox} {entry.get('content', '')}")
                new_text = "\n".join(plan_lines)
                await self._update_progress(
                    owner, repo, progress_id, new_text, installation_id, is_review
                )
                session_data["current_text"] = new_text

        except Exception:
            logger.exception("Error sending update to GitHub for %s", session_id)

    async def send_completion(self, session_id: str, message: str) -> None:
        """Send completion: final edit + rocket reaction."""
        session_data = self._sessions.get(session_id)
        if not session_data:
            logger.warning("No session data for %s, cannot send completion", session_id)
            return

        final_text = self._message_buffers.pop(session_id, "") or message

        try:
            owner = session_data["owner"]
            repo = session_data["repo"]
            installation_id = session_data["installation_id"]
            progress_id = session_data["progress_comment_id"]
            trigger_id = session_data["trigger_comment_id"]
            is_review = session_data["is_review_comment"]

            # Update progress comment with final response
            await self._update_progress(
                owner, repo, progress_id, final_text, installation_id, is_review
            )

            # Add rocket reaction to the trigger comment or issue
            if trigger_id:
                await self._api.create_reaction(
                    owner,
                    repo,
                    trigger_id,
                    "rocket",
                    installation_id,
                    is_review_comment=is_review,
                )
            elif session_data.get("trigger_issue_number"):
                await self._api.create_issue_reaction(
                    owner,
                    repo,
                    session_data["trigger_issue_number"],
                    "rocket",
                    installation_id,
                )

            logger.info("Completed session %s", session_id)

        except Exception:
            logger.exception("Error sending completion to GitHub for %s", session_id)

    async def send_error(self, session_id: str, error: str) -> None:
        """Send error message in comment + confused reaction."""
        session_data = self._sessions.get(session_id)
        self._message_buffers.pop(session_id, None)

        if not session_data:
            logger.warning("No session data for %s, cannot send error", session_id)
            return

        try:
            owner = session_data["owner"]
            repo = session_data["repo"]
            installation_id = session_data["installation_id"]
            progress_id = session_data["progress_comment_id"]
            trigger_id = session_data["trigger_comment_id"]
            is_review = session_data["is_review_comment"]

            error_text = f"**Error:** {error}"
            await self._update_progress(
                owner, repo, progress_id, error_text, installation_id, is_review
            )

            # Add confused reaction to the trigger comment or issue
            if trigger_id:
                await self._api.create_reaction(
                    owner,
                    repo,
                    trigger_id,
                    "confused",
                    installation_id,
                    is_review_comment=is_review,
                )
            elif session_data.get("trigger_issue_number"):
                await self._api.create_issue_reaction(
                    owner,
                    repo,
                    session_data["trigger_issue_number"],
                    "confused",
                    installation_id,
                )

            logger.error("Error in session %s: %s", session_id, error)

        except Exception:
            logger.exception("Error sending error to GitHub for %s", session_id)

    async def _update_progress(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        body: str,
        installation_id: int,
        is_review_comment: bool,
    ) -> None:
        """Update a progress comment (issue comment or review comment)."""
        if is_review_comment:
            await self._api.update_review_comment(owner, repo, comment_id, body, installation_id)
        else:
            await self._api.update_comment(owner, repo, comment_id, body, installation_id)

    async def close(self) -> None:
        """Clean up resources."""
        await self._api.close()
        await self._auth.close()
        logger.info("GitHub adapter closed")
