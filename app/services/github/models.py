"""Pydantic models for GitHub webhook payloads."""

from __future__ import annotations

from pydantic import BaseModel


class GitHubUser(BaseModel):
    id: int
    login: str
    type: str = "User"  # "User" | "Bot"


class GitHubRepository(BaseModel):
    id: int
    full_name: str
    name: str


class GitHubIssue(BaseModel):
    number: int
    title: str
    body: str | None = None
    html_url: str
    pull_request: dict | None = None  # Present if the issue is a PR


class GitHubComment(BaseModel):
    id: int
    body: str
    user: GitHubUser
    html_url: str


class GitHubPullRequest(BaseModel):
    number: int
    title: str
    body: str | None = None
    html_url: str


class GitHubReviewComment(BaseModel):
    """A review comment on a pull request (inline on a diff)."""

    id: int
    body: str
    user: GitHubUser
    html_url: str
    path: str | None = None
    diff_hunk: str | None = None
    line: int | None = None


class GitHubInstallation(BaseModel):
    id: int


class IssueCommentPayload(BaseModel):
    """Payload for issue_comment webhook events."""

    action: str  # "created" | "edited" | "deleted"
    issue: GitHubIssue
    comment: GitHubComment
    repository: GitHubRepository
    installation: GitHubInstallation | None = None
    sender: GitHubUser


class IssuesPayload(BaseModel):
    """Payload for issues webhook events."""

    action: str  # "opened" | "edited" | "closed" | etc.
    issue: GitHubIssue
    repository: GitHubRepository
    installation: GitHubInstallation | None = None
    sender: GitHubUser


class PullRequestReviewCommentPayload(BaseModel):
    """Payload for pull_request_review_comment webhook events."""

    action: str  # "created" | "edited" | "deleted"
    pull_request: GitHubPullRequest
    comment: GitHubReviewComment
    repository: GitHubRepository
    installation: GitHubInstallation | None = None
    sender: GitHubUser
