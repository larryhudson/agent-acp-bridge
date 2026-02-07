"""Async Linear GraphQL API client."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.linear.models import PlanStep

logger = logging.getLogger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"


class LinearApiClient:
    """Async client for the Linear GraphQL API."""

    def __init__(self, access_token: str):
        self._access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=LINEAR_API_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """Execute a GraphQL query/mutation."""
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        response = await self._client.post("", json=payload)
        response.raise_for_status()
        result = response.json()

        if "errors" in result:
            logger.error("GraphQL errors: %s", result["errors"])
            raise RuntimeError(f"GraphQL errors: {result['errors']}")

        return result.get("data", {})

    async def create_activity(
        self,
        session_id: str,
        *,
        activity_type: str,
        body: str | None = None,
        action: str | None = None,
        parameter: str | None = None,
        result: str | None = None,
        ephemeral: bool = False,
        signal: str | None = None,
        signal_metadata: dict | None = None,
    ) -> dict:
        """Create an agent activity on a session.

        activity_type: "thought" | "action" | "elicitation" | "response" | "error"
        """
        content: dict[str, Any] = {"type": activity_type}

        if activity_type == "action":
            if action:
                content["action"] = action
            content["parameter"] = parameter or ""
            if result:
                content["result"] = result
        else:
            if body:
                content["body"] = body

        variables: dict[str, Any] = {
            "input": {
                "agentSessionId": session_id,
                "content": content,
            }
        }

        if ephemeral and activity_type in ("thought", "action"):
            variables["input"]["ephemeral"] = True

        if signal:
            variables["input"]["signal"] = signal
        if signal_metadata:
            variables["input"]["signalMetadata"] = signal_metadata

        query = """
        mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
            agentActivityCreate(input: $input) {
                success
                agentActivity {
                    id
                }
            }
        }
        """
        return await self._graphql(query, variables)

    async def update_session_plan(self, session_id: str, steps: list[PlanStep]) -> dict:
        """Update the plan for an agent session."""
        plan = [{"content": s.content, "status": s.status} for s in steps]
        query = """
        mutation AgentSessionUpdate($agentSessionId: String!, $data: AgentSessionUpdateInput!) {
            agentSessionUpdate(id: $agentSessionId, input: $data) {
                success
            }
        }
        """
        return await self._graphql(
            query,
            {
                "agentSessionId": session_id,
                "data": {"plan": plan},
            },
        )

    async def update_session_urls(self, session_id: str, urls: list[dict[str, str]]) -> dict:
        """Update external URLs for an agent session.

        urls: [{"label": "...", "url": "..."}]
        """
        query = """
        mutation AgentSessionUpdate($agentSessionId: String!, $data: AgentSessionUpdateInput!) {
            agentSessionUpdate(id: $agentSessionId, input: $data) {
                success
            }
        }
        """
        return await self._graphql(
            query,
            {
                "agentSessionId": session_id,
                "data": {"externalUrls": urls},
            },
        )

    async def get_started_state(self, team_id: str) -> dict | None:
        """Get the first 'started' workflow state for a team (lowest position)."""
        query = """
        query TeamStartedStatuses($teamId: String!) {
            team(id: $teamId) {
                states(filter: { type: { eq: "started" } }) {
                    nodes {
                        id
                        name
                        position
                    }
                }
            }
        }
        """
        data = await self._graphql(query, {"teamId": team_id})
        states = data.get("team", {}).get("states", {}).get("nodes", [])
        if not states:
            return None
        return min(states, key=lambda s: s["position"])

    async def update_issue_state(self, issue_id: str, state_id: str) -> dict:
        """Update an issue's workflow state."""
        query = """
        mutation IssueUpdate($issueId: String!, $stateId: String!) {
            issueUpdate(id: $issueId, input: { stateId: $stateId }) {
                success
            }
        }
        """
        return await self._graphql(
            query,
            {
                "issueId": issue_id,
                "stateId": state_id,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()
