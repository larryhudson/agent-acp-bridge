# Linear Agents API - Comprehensive Reference

This document contains all the details needed to implement a Linear agent adapter,
extracted from the official Linear developer documentation.

---

## Table of Contents

1. [Agent Interaction Guidelines (AIG)](#1-agent-interaction-guidelines)
2. [Getting Started with Agents](#2-getting-started-with-agents)
3. [Developing the Agent Interaction](#3-developing-the-agent-interaction)
4. [Interaction Best Practices](#4-interaction-best-practices)
5. [Signals](#5-signals)
6. [OAuth 2.0 Authentication](#6-oauth-20-authentication)
7. [Webhooks](#7-webhooks)

---

## 1. Agent Interaction Guidelines

Source: https://linear.app/developers/aig.md

These are the behavioral principles all Linear agents must follow. Linear describes
this as "a living document."

### Core Principles

1. **Identity Disclosure**: An agent must always disclose that it is an agent. Agents
   are clearly marked with a small badge so humans instantly recognize they are
   interacting with a non-human entity.

2. **Native Platform Integration**: An agent should inhabit the platform natively.
   It operates through existing UI patterns and can use the same actions a human
   user would.

3. **Instant Feedback**: An agent should provide instant feedback. Upon invocation,
   agents must provide prompt but unobtrusive signals (e.g., a "Thinking" indicator).

4. **State Transparency**: An agent should be clear and transparent about its
   internal state. Users should understand what is happening at a glance and be able
   to inspect the underlying reasoning, tool calls, prompts, and decision logic.

5. **Respect Disengagement**: An agent should respect requests to disengage. When a
   user asks the agent to stop, it must step back immediately and only re-engage
   once it has received a clear signal.

6. **Human Accountability**: An agent cannot be held accountable. Tasks may be
   delegated to agents but the final responsibility should always remain with a
   human. Clear delegation structure between agents and human overseers is required.

---

## 2. Getting Started with Agents

Source: https://linear.app/developers/agents.md

### Overview

- Linear Agent APIs are in **Developer Preview**.
- Agents function as workspace members.
- Agents are installable at no cost and do not count toward billable user limits.
- Reference implementation: [Weather Bot](https://github.com/linear/weather-bot)
  (TypeScript SDK + Cloudflare).

### Authentication: OAuth2 with `actor=app`

Agents use the standard Linear OAuth2 flow with the special parameter `actor=app`
(this supersedes any references to `actor=application`).

**Key OAuth scopes for agents:**

| Scope | Description |
|-------|-------------|
| `app:assignable` | Allow the app to be assigned as a delegate on issues and made a member of projects |
| `app:mentionable` | Allow the app to be mentioned in issues, documents, and other editor surfaces |
| `customer:read` | Read-only access to customer entities |
| `customer:write` | Read/write access to customer entities |
| `initiative:read` | Read-only access to initiative entities |
| `initiative:write` | Read/write access to initiative entities |

**Important**: Integrations using `actor=app` mode cannot also request `admin` scope.

### Installation Process

1. Create an Application in Linear with webhooks enabled.
2. Select the webhook categories:
   - **Agent session events** (required) - notifications when events occur relevant to the app
   - **Inbox notifications** (recommended)
   - **Permission changes** (recommended) - sends `PermissionChange` webhook when access changes
3. Complete the OAuth flow with `actor=app`.
4. Store the workspace-specific app ID obtained via:

```graphql
query Me {
  viewer {
    id
  }
}
```

This returns the unique app ID for each workspace installation using the OAuth access token.

### Initial Interaction Flow

When a user **delegates** (assigns) an issue to your agent or **mentions** it, Linear
triggers a `created` `AgentSessionEvent` webhook containing an `agentSession` object.

The agent must:
- **Emit a `thought` activity within 10 seconds** to acknowledge the session has begun.
- Use the `promptContext` field to construct a formatted string containing the session's
  relevant context (issue details, comments, guidance).

### Key Terminology

- **Delegate** (not "assignee") - the assignment target for agent issues.
- `viewer.id` - the workspace-unique app identifier.

---

## 3. Developing the Agent Interaction

Source: https://linear.app/developers/agent-interaction.md

### Agent Sessions

#### Lifecycle & States

Agent sessions automatically track when an agent is mentioned or assigned an issue.
Sessions progress through five states:

| State | Description |
|-------|-------------|
| `pending` | Session created, awaiting agent response |
| `active` | Agent is actively working |
| `error` | Agent encountered an error |
| `awaitingInput` | Agent needs user input |
| `complete` | Agent has finished |

**Linear manages state transitions automatically based on emitted activities** - no
manual state management is needed. For example:
- Emitting a `thought` or `action` transitions to `active`
- Emitting an `elicitation` transitions to `awaitingInput`
- Emitting a `response` transitions to `complete`
- Emitting an `error` transitions to `error`

#### External URLs

Agents can set `externalUrls` on sessions via the `agentSessionUpdate` mutation. This
allows users to access agent dashboards and **prevents sessions from being marked
unresponsive**.

```graphql
mutation AgentSessionUpdate($agentSessionId: String!, $data: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $agentSessionId, input: $data) {
    success
  }
}
```

Variables for external URLs:

```json
{
  "data": {
    "externalUrls": [
      {
        "label": "Agent Dashboard",
        "url": "https://agent.example.com/session/123"
      }
    ]
  }
}
```

You can also use incremental modifications:
- `addedExternalUrls` - add URLs without replacing existing ones
- `removedExternalUrls` - remove specific URLs

**Pull Request Integration**: Agents can add GitHub PR URLs to `externalUrls` to inform
users of published pull requests, unlocking future PR-related features.

### Webhook Events: AgentSessionEvent

Two action types exist:

#### `created` Action

Triggered when a new Agent Session is created (by user mention or delegation).

Payload fields:
- `agentSession.issue` - the issue object
- `agentSession.comment` - the comment that triggered the session (if applicable)
- `previousComments` - prior comments on the issue
- `guidance` - guidance rules/instructions
- `promptContext` - a pre-formatted string containing all relevant session context
  (issue details, comment threads, and guidance rules in an XML-like structure)

**The agent must respond within 10 seconds** with a `thought` activity.

#### `prompted` Action

Triggered when a user sends a new message into an existing Agent Session.

The user's message appears in the `agentActivity.body` field of the webhook payload.

**The agent must respond to webhooks within 5 seconds** (HTTP response).

### Agent Activities

Activities are the primary way agents communicate. Create them using the
`agentActivityCreate` mutation.

#### GraphQL Mutation

```graphql
mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) {
    success
    agentActivity {
      id
    }
  }
}
```

#### TypeScript SDK

```typescript
const { success, agentActivity } = await linearClient.createAgentActivity({
  agentSessionId: "...",
  content: {
    type: "...",
    // ... other payload fields
  },
});
```

#### Five Activity Content Types

**1. `thought` - Internal reasoning**

```json
{
  "input": {
    "agentSessionId": "session-id",
    "content": {
      "type": "thought",
      "body": "The user asked about the weather."
    }
  }
}
```

**2. `elicitation` - Request user clarification**

```json
{
  "input": {
    "agentSessionId": "session-id",
    "content": {
      "type": "elicitation",
      "body": "Where are you located? I will find the current weather for you"
    }
  }
}
```

**3. `action` - Tool invocation (without result)**

```json
{
  "input": {
    "agentSessionId": "session-id",
    "content": {
      "type": "action",
      "action": "Searching",
      "parameter": "San Francisco Weather"
    }
  }
}
```

**3b. `action` - Tool invocation (with result)**

```json
{
  "input": {
    "agentSessionId": "session-id",
    "content": {
      "type": "action",
      "action": "Searched",
      "parameter": "San Francisco Weather",
      "result": "12°C, mostly clear"
    }
  }
}
```

**4. `response` - Completed work announcement**

```json
{
  "input": {
    "agentSessionId": "session-id",
    "content": {
      "type": "response",
      "body": "The weather in San Francisco is currently **foggy**, no surprise there."
    }
  }
}
```

**5. `error` - Failure reporting (supports Markdown with links for remediation)**

```json
{
  "input": {
    "agentSessionId": "session-id",
    "content": {
      "type": "error",
      "body": "Out of credits. [Pay up!](https://agent.com/pay)"
    }
  }
}
```

#### Activity Content GraphQL Types

When querying activities, content uses union types:

- `AgentActivityThoughtContent` - has `body` field
- `AgentActivityActionContent` - has `action`, `parameter`, `result` fields
- `AgentActivityElicitationContent` - has `body` field
- `AgentActivityResponseContent` - has `body` field
- `AgentActivityErrorContent` - has `body` field
- `AgentActivityPromptContent` - has `body` field (user messages)

#### Ephemeral Activities

Only `thought` and `action` type activities can be marked ephemeral. Ephemeral
activities are displayed temporarily and will be replaced when the next activity
arrives.

#### Mentions in Content

Plain Linear URLs in Markdown will be converted into mentions in the UI. For example,
`https://linear.app/linear/profiles/user` becomes `@user`.

### Agent Plans

Plans provide session-level task checklists with evolving statuses.

#### Plan Step Structure

```typescript
interface PlanStep {
  content: string;    // Task description
  status: "pending" | "inProgress" | "completed" | "canceled";
}
```

#### Updating Plans

Plans must be **replaced entirely** when updating - individual item modifications
are not supported. Use the `agentSessionUpdate` mutation:

```graphql
mutation AgentSessionUpdate($agentSessionId: String!, $data: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $agentSessionId, input: $data) {
    success
  }
}
```

Variables:

```json
{
  "agentSessionId": "session-id",
  "data": {
    "plan": [
      {
        "content": "Update @linear/sdk to v61.0.0 and run npm install",
        "status": "inProgress"
      },
      {
        "content": "Implement agent plan mutations",
        "status": "pending"
      }
    ]
  }
}
```

### Repository Suggestions

Use `issueRepositorySuggestions` query to get ranked repository matches:

```graphql
query($issueId: String!, $agentSessionId: String!) {
  issueRepositorySuggestions(
    issueId: $issueId
    agentSessionId: $agentSessionId
    candidateRepositories: [
      {
        hostname: "github.com",
        repositoryFullName: "linear/linear-app"
      }
    ]
  ) {
    suggestions {
      repositoryFullName
      hostname
      confidence
    }
  }
}
```

This returns filtered suggestions with confidence scores, helping agents proceed
confidently or prompt users for clarification.

---

## 4. Interaction Best Practices

Source: https://linear.app/developers/agent-best-practices.md

### Critical Timing Requirements

| Requirement | Deadline |
|-------------|----------|
| First `thought` activity after `created` webhook | **Within 10 seconds** |
| HTTP response to webhook | **Within 5 seconds** |
| Session activity window before staleness | **30 minutes** |

- If 30 minutes pass without activity, the session becomes stale. It can be recovered
  by sending another activity.
- Setting `externalUrls` on the session prevents it from being marked unresponsive.

### Status Management

When delegated to an issue that is NOT in `started`, `completed`, or `canceled` status,
the agent should move the issue to the first status in `started` by:

1. Querying workflow states filtered by type `started`
2. Selecting the one with the lowest `position` value

```graphql
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
```

Then update the issue to use the state with the lowest `position` value.

### Delegation & Ownership

If no `Issue.delegate` exists during implementation work, the agent should set itself
as the delegate to make the agent's role in the issue more explicit.

### Activity Responses

Upon completion, emit the appropriate activity type:
- `response` - work is complete
- `elicitation` - additional user actions needed
- `error` - issues encountered

### Data Source Priority

**Comments may not be reliable to read from**, as they are editable and may have
changed since your agent's last run. Instead, rely on **Agent Activities** as
frozen-in-time snapshots.

#### Querying Agent Session Activities

```graphql
query AgentSession($agentSessionId: String!) {
  agentSession(id: $agentSessionId) {
    activities {
      edges {
        node {
          updatedAt
          content {
            ... on AgentActivityThoughtContent {
              body
            }
            ... on AgentActivityActionContent {
              action
              parameter
              result
            }
            ... on AgentActivityElicitationContent {
              body
            }
            ... on AgentActivityResponseContent {
              body
            }
            ... on AgentActivityErrorContent {
              body
            }
            ... on AgentActivityPromptContent {
              body
            }
          }
        }
      }
    }
  }
}
```

Requires TypeScript SDK v53.0.0+.

### Additional Webhook Categories

#### Inbox Notifications

Triggered when users interact with agents.

**Payload structure:**

```json
{
  "type": "AppUserNotification",
  "action": "<NotificationType>",
  "createdAt": "ISO 8601 timestamp",
  "organizationId": "uuid",
  "oauthClientId": "uuid",
  "appUserId": "uuid",
  "notification": { ... }
}
```

**Action (notification) types:**
- `issueMention` - agent mentioned in an issue
- `issueEmojiReaction` - emoji reaction on agent's content
- `issueCommentMention` - agent mentioned in a comment
- `issueCommentReaction` - reaction on agent's comment
- `issueAssignedToYou` - issue assigned/delegated to agent
- `issueUnassignedFromYou` - issue unassigned from agent
- `issueNewComment` - new comment on an issue the agent is involved with
- `issueStatusChanged` - status change on an issue the agent is involved with

#### Permission Changes

**Payload structure:**

```json
{
  "type": "PermissionChange",
  "action": "teamAccessChanged",
  "createdAt": "ISO 8601 timestamp",
  "organizationId": "uuid",
  "oauthClientId": "uuid",
  "appUserId": "uuid",
  "canAccessAllPublicTeams": true,
  "addedTeamIds": ["team-uuid-1"],
  "removedTeamIds": ["team-uuid-2"],
  "webhookTimestamp": 1234567890,
  "webhookId": "uuid"
}
```

#### OAuth Revocation

```json
{
  "type": "OAuthApp",
  "action": "revoked",
  "oauthClientId": "uuid",
  "organizationId": "uuid"
}
```

---

## 5. Signals

Source: https://linear.app/developers/agent-signals.md

Signals are optional metadata that modify how an Agent Activity should be interpreted
or handled by the recipient. Both agents and human users can attach signals.

### Human-to-Agent Signals

#### `stop` Signal

- **Applicable to**: `prompt`-type activities
- **Purpose**: Instructs the agent to halt work immediately
- **Behavior**: When received, agents must:
  1. Cease all actions including code modifications, updates, and API calls
  2. Emit a final `response` or `error` activity confirming they have stopped
  3. Report their current state
- Users can trigger this from within Linear's interface

### Agent-to-Human Signals

#### `auth` Signal

- **Applicable to**: `elicitation`-type activities
- **Purpose**: Indicates the agent requires the user to complete an account linking
  process before it can continue
- **Behavior**: Linear displays a temporary UI with an authentication link that
  dismisses upon receiving newer agent activities

**Mutation payload:**

```json
{
  "agentSessionId": "session-id",
  "content": {
    "type": "elicitation",
    "body": "Please authenticate to continue"
  },
  "signal": "auth",
  "signalMetadata": {
    "url": "https://auth.example.com/oauth",
    "userId": "optional-user-id",
    "providerName": "Orbit"
  }
}
```

**`signalMetadata` fields:**
- `url` (required) - authentication endpoint URL
- `userId` (optional) - restricts auth to a specific user
- `providerName` (optional) - identifies the provider name displayed in UI

After the user completes authentication, the agent should resume by emitting a
`thought` activity.

#### `select` Signal

- **Applicable to**: `elicitation`-type activities
- **Purpose**: Presents a list of options for the user to choose from
- **Behavior**: Users can bypass selection by replying in free text, which dismisses
  the elicitation. Selected options generate standard `prompt` activities.

**Mutation payload:**

```json
{
  "agentSessionId": "session-id",
  "content": {
    "type": "elicitation",
    "body": "Which repository is this issue about?"
  },
  "signal": "select",
  "signalMetadata": {
    "options": [
      { "value": "https://github.com/YOUR-ORG/YOUR-REPOSITORY" },
      { "value": "https://github.com/YOUR-ORG/ANOTHER-REPOSITORY" }
    ]
  }
}
```

**`signalMetadata` fields:**
- `options` (required) - array of objects, each with a `value` property
- GitHub URLs receive automatic enrichment with icons and formatted names

**Important**: Human responses may include natural language (not just the option value),
requiring LLM interpretation.

### Signal Summary Table

| Signal | Direction | Activity Type | Purpose |
|--------|-----------|---------------|---------|
| `stop` | Human -> Agent | `prompt` | Halt work immediately |
| `auth` | Agent -> Human | `elicitation` | Request account linking/authentication |
| `select` | Agent -> Human | `elicitation` | Present options for user selection |

---

## 6. OAuth 2.0 Authentication

Source: https://linear.app/developers/oauth-2-0-authentication

### Authorization Endpoint

**URL**: `https://linear.app/oauth/authorize`
**Method**: GET

**Required Parameters:**
- `client_id` - Application identifier
- `redirect_uri` - Callback destination
- `response_type=code` - Must be "code"
- `scope` - Comma-separated permission list
- `actor=app` - **Required for agents** (not `actor=user` which is the default)

**Optional Parameters:**
- `state` - CSRF protection (should always be supplied)
- `prompt=consent` - Forces consent screen display

**PKCE Support:**
- `code_challenge` - Generated challenge value
- `code_challenge_method` - Either `plain` or `S256`

### Token Exchange Endpoint

**URL**: `https://api.linear.app/oauth/token`
**Method**: POST
**Content-Type**: `application/x-www-form-urlencoded`

**Required Parameters:**
- `code` - Authorization code from redirect
- `redirect_uri` - Must match original URI
- `client_id` - Application ID
- `client_secret` - Application secret
- `grant_type=authorization_code`

**PKCE**: Substitute `code_verifier` (required) for `client_secret` (optional).

### Token Response (with refresh tokens)

```json
{
  "access_token": "00a21d8b...",
  "token_type": "Bearer",
  "expires_in": 86399,
  "scope": "read write",
  "refresh_token": "sz0c8ffy..."
}
```

`expires_in` is 86399 seconds (~24 hours) for apps with refresh tokens.

### Token Response (legacy, without refresh tokens)

```json
{
  "access_token": "00a21d8b...",
  "token_type": "Bearer",
  "expires_in": 315705599,
  "scope": "read write"
}
```

### Token Refresh

**URL**: `https://api.linear.app/oauth/token`
**Method**: POST

**Parameters:**
- `refresh_token` - Token from previous response
- `grant_type=refresh_token`
- `client_id` - Optional if using HTTP basic auth
- `client_secret` - Optional if using HTTP basic auth or PKCE

**Authentication Options:**
1. HTTP Basic: `Authorization: Basic <base64(client_id:client_secret)>`
2. Form parameters

### Token Revocation

**URL**: `https://api.linear.app/oauth/revoke`
**Method**: POST
**Body**: URL-encoded with `token` field; optional `token_type_hint`

### Client Credentials Grant

**URL**: `https://api.linear.app/oauth/token`

**Parameters:**
- `grant_type=client_credentials`
- `scope` - Required, comma-separated
- `client_id` / `client_secret`

Token validity: 30 days. Only one active client credentials token at a time.

### API Request Headers

```
Authorization: Bearer <ACCESS_TOKEN>
Content-Type: application/json
```

The API endpoint is `https://api.linear.app/graphql`.

---

## 7. Webhooks

Source: https://linear.app/developers/webhooks

### Delivery

- HTTP POST to publicly accessible HTTPS URLs
- Endpoint must respond with **HTTP 200 within 5 seconds**
- **Retry policy**: Failed deliveries retry max 3 times with backoff: 1 minute, 1 hour, 6 hours
- Unresponsive webhooks may be disabled automatically

### HTTP Headers

```
Accept-Charset: utf-8
Content-Type: application/json; charset=utf-8
Linear-Delivery: <UUID v4>        # Unique payload identifier
Linear-Event: <Entity type>       # Entity type (Issue, Comment, etc.)
Linear-Signature: <HMAC-SHA256>   # Hex-encoded signature
User-Agent: Linear-Webhook
```

### Signature Verification

The `Linear-Signature` header contains an HMAC-SHA256 hex digest of the raw request
body, signed with the webhook's secret key.

```typescript
const crypto = require("node:crypto");

function verifySignature(headerSignatureString: string, rawBody: Buffer): boolean {
  if (typeof headerSignatureString !== "string") {
    return false;
  }
  const headerSignature = Buffer.from(headerSignatureString, "hex");
  const computedSignature = crypto
    .createHmac("sha256", LINEAR_WEBHOOK_SECRET)
    .update(rawBody)
    .digest();
  return crypto.timingSafeEqual(computedSignature, headerSignature);
}
```

**Timestamp validation**: Verify `webhookTimestamp` (UNIX milliseconds) is within
60 seconds of current time to guard against replay attacks.

### General Webhook Payload Structure (Data Change Events)

```json
{
  "action": "create | update | remove",
  "type": "Entity type",
  "actor": {
    "id": "string",
    "type": "user | OauthClient | Integration",
    "name": "string",
    "email": "string",
    "url": "string"
  },
  "createdAt": "ISO 8601 timestamp",
  "data": { "...serialized entity..." },
  "url": "Linear entity URL",
  "updatedFrom": { "...previous values for update actions..." },
  "organizationId": "UUID",
  "webhookTimestamp": 1234567890,
  "webhookId": "UUID"
}
```

### Webhook Management GraphQL

**Create:**

```graphql
mutation {
  webhookCreate(
    input: {
      url: "https://example.com/webhooks/linear"
      teamId: "team-uuid"
      resourceTypes: ["Issue"]
    }
  ) {
    success
    webhook {
      id
      enabled
    }
  }
}
```

**Query:**

```graphql
query {
  webhooks {
    nodes {
      id
      url
      enabled
      team { id name }
    }
  }
}
```

**Delete:**

```graphql
mutation {
  webhookDelete(id: "webhook-uuid") {
    success
  }
}
```

### Authorized IP Addresses

Linear sends webhooks from:
- 35.231.147.226
- 35.243.134.228
- 34.140.253.14
- 34.38.87.206
- 34.134.222.122
- 35.222.25.142

---

## Quick Reference: Complete Agent Interaction Flow

### 1. Setup Phase
1. Create a Linear Application with webhooks enabled (Agent session events, Inbox notifications, Permission changes)
2. Implement OAuth flow with `actor=app` and required scopes (`app:assignable`, `app:mentionable`, plus any data scopes)
3. Exchange code for tokens at `https://api.linear.app/oauth/token`
4. Query `viewer { id }` to get the workspace-specific app ID
5. Store access token, refresh token, and app ID per workspace

### 2. Receiving Work (Webhook: `AgentSessionEvent` with action `created`)
1. Respond to webhook HTTP request within **5 seconds** with 200
2. Verify `Linear-Signature` header using HMAC-SHA256
3. Extract `agentSession`, `promptContext`, `previousComments`, `guidance`
4. Emit a `thought` activity within **10 seconds** of the webhook

### 3. Doing Work
1. Set `externalUrls` on the session (prevents unresponsive marking)
2. Move issue to first `started` status if not already in `started`/`completed`/`canceled`
3. Set self as `Issue.delegate` if no delegate exists
4. Create a plan with steps (`agentSessionUpdate` with `plan` array)
5. Emit `action` activities for tool invocations
6. Update plan steps as work progresses (replace entire plan array)
7. Keep emitting activities within 30-minute window to avoid staleness

### 4. Requesting Input
1. Emit `elicitation` activity with `body` describing what is needed
2. Optionally use `select` signal with `signalMetadata.options` for choices
3. Optionally use `auth` signal with `signalMetadata.url` for authentication
4. Session transitions to `awaitingInput` state

### 5. Receiving User Messages (Webhook: `AgentSessionEvent` with action `prompted`)
1. User's message is in `agentActivity.body`
2. Check for `stop` signal - if present, halt immediately and emit `response` or `error`
3. Otherwise, process the message and continue work

### 6. Completing Work
1. Emit `response` activity when work is complete
2. Emit `error` activity if something went wrong
3. Session transitions to `complete` or `error` state

### Key Timing Summary

| Event | Deadline |
|-------|----------|
| HTTP response to any webhook | 5 seconds |
| First `thought` after `created` webhook | 10 seconds |
| Activity before session goes stale | 30 minutes |
| Access token expiry (with refresh) | ~24 hours |
| Client credentials token validity | 30 days |
