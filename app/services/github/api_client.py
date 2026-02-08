"""Async GitHub REST API client."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.github.auth import GitHubAuth

logger = logging.getLogger(__name__)


class GitHubApiClient:
    """Async client for GitHub REST API.

    Handles:
    - Creating and updating issue comments
    - Creating and updating PR review comment replies
    - Adding reactions to comments
    - Auth via GitHubAuth installation tokens
    """

    def __init__(self, auth: GitHubAuth) -> None:
        self._auth = auth
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Accept": "application/vnd.github+json"},
            timeout=30.0,
        )

    async def _headers(self, installation_id: int) -> dict[str, str]:
        """Get auth headers for an installation."""
        token = await self._auth.get_installation_token(installation_id)
        return {"Authorization": f"Bearer {token}"}

    async def create_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str,
        installation_id: int,
    ) -> dict[str, Any]:
        """Create a comment on an issue or PR.

        POST /repos/{owner}/{repo}/issues/{issue_number}/comments
        """
        headers = await self._headers(installation_id)
        response = await self._client.post(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            headers=headers,
            json={"body": body},
        )
        response.raise_for_status()
        return response.json()

    async def update_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        body: str,
        installation_id: int,
    ) -> dict[str, Any]:
        """Update an issue comment.

        PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}
        """
        headers = await self._headers(installation_id)
        response = await self._client.patch(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            headers=headers,
            json={"body": body},
        )
        response.raise_for_status()
        return response.json()

    async def create_review_comment_reply(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        comment_id: int,
        body: str,
        installation_id: int,
    ) -> dict[str, Any]:
        """Reply to a PR review comment thread.

        POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies
        """
        headers = await self._headers(installation_id)
        response = await self._client.post(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies",
            headers=headers,
            json={"body": body},
        )
        response.raise_for_status()
        return response.json()

    async def update_review_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        body: str,
        installation_id: int,
    ) -> dict[str, Any]:
        """Update a PR review comment.

        PATCH /repos/{owner}/{repo}/pulls/comments/{comment_id}
        """
        headers = await self._headers(installation_id)
        response = await self._client.patch(
            f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
            headers=headers,
            json={"body": body},
        )
        response.raise_for_status()
        return response.json()

    async def create_reaction(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        reaction: str,
        installation_id: int,
        *,
        is_review_comment: bool = False,
    ) -> None:
        """Add an emoji reaction to a comment.

        For issue comments: POST /repos/{owner}/{repo}/issues/comments/{comment_id}/reactions
        For review comments: POST /repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions
        """
        headers = await self._headers(installation_id)
        if is_review_comment:
            url = f"/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions"
        else:
            url = f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"

        response = await self._client.post(
            url,
            headers=headers,
            json={"content": reaction},
        )
        # 200 OK = already existed, 201 Created = new reaction — both are fine
        if response.status_code not in (200, 201):
            logger.warning(
                "Failed to add reaction %s to comment %d: %s",
                reaction,
                comment_id,
                response.text,
            )

    async def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        installation_id: int,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """List comments on an issue or PR.

        GET /repos/{owner}/{repo}/issues/{issue_number}/comments
        """
        headers = await self._headers(installation_id)
        response = await self._client.get(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            headers=headers,
            params={"per_page": per_page},
        )
        response.raise_for_status()
        return response.json()

    async def create_issue_reaction(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        reaction: str,
        installation_id: int,
    ) -> None:
        """Add an emoji reaction to an issue.

        POST /repos/{owner}/{repo}/issues/{issue_number}/reactions
        """
        headers = await self._headers(installation_id)
        url = f"/repos/{owner}/{repo}/issues/{issue_number}/reactions"
        response = await self._client.post(
            url,
            headers=headers,
            json={"content": reaction},
        )
        if response.status_code not in (200, 201):
            logger.warning(
                "Failed to add reaction %s to issue %d: %s",
                reaction,
                issue_number,
                response.text,
            )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
