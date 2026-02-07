# Manual Testing Plan

## Prerequisites

- Docker installed (and Docker Desktop running)
- A Linear workspace with an OAuth app configured for agent sessions
- A publicly accessible URL for webhooks (e.g., ngrok, Cloudflare Tunnel)
- An `ANTHROPIC_API_KEY` or a `CLAUDE_CODE_OAUTH_TOKEN` for Claude authentication
- A git repository cloned into `./data/projects/` for the agent to work on

## 1. Environment Setup

### 1.1 Configure `.env`

```bash
cp .env.example .env
```

Fill in all values:

```env
# Authentication — set ONE of these:
ANTHROPIC_API_KEY=sk-ant-...
# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...

ACP_AGENT_COMMAND=claude-code-acp
ENABLED_SERVICES=linear
PROJECT_MAPPINGS_JSON={"linear:TEAM_ID_HERE": "/data/projects/my-repo"}

LINEAR_WEBHOOK_SECRET=lin_wh_...
LINEAR_ACCESS_TOKEN=lin_api_...
LINEAR_AGENT_ID=your-linear-app-client-id
```

**Authentication note:** The bridge passes all environment variables through to the
`claude-code-acp` subprocess (via standard process inheritance in
`app/acp/session.py`). Either `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` will
work — set whichever you have.

**`LINEAR_AGENT_ID`:** This is currently unused by the bridge code but reserved for
future use. You can set it to your Linear OAuth app's client ID.

**Gotcha — `#` in tokens:** If your token contains a `#` character, wrap it in
double quotes in the `.env` file, otherwise everything after `#` is treated as a
comment and the token gets truncated.

**Gotcha — reloading `.env`:** `docker compose restart` does NOT re-read the `.env`
file. You must run `docker compose down && docker compose up` (or `docker compose up
--build` if code changed too) to pick up `.env` changes.

### Finding your Linear team ID

Query the Linear API with your access token:

```bash
curl -s -X POST "https://api.linear.app/graphql" \
  -H "Content-Type: application/json" \
  -H "Authorization: YOUR_LINEAR_ACCESS_TOKEN" \
  -d '{"query":"{ teams { nodes { id name key } } }"}' | python3 -m json.tool
```

Use the `id` value from the response as `TEAM_ID_HERE` in `PROJECT_MAPPINGS_JSON`.

### 1.2 Build and Start

```bash
docker compose up --build
```

Verify the container starts without errors in the logs. You should see:

```
Registered adapter: linear
ACP Bridge started (services: linear)
```

### 1.3 Start a Tunnel

In a separate terminal, expose port 8000:

```bash
# ngrok (with custom domain)
ngrok http 8000 --domain=your-domain.ngrok-free.app

# or Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8000
```

Note the public URL — you'll configure it in Linear.

### 1.4 Configure Linear Webhook

In your Linear app settings, set the webhook URL to:

```
https://YOUR_TUNNEL_URL/webhooks/linear
```

Make sure **Agent session events** is selected as a webhook category.

---

## 2. Health Check

### Test

```bash
curl http://localhost:8000/health
```

### Expected

```json
{"status": "ok", "services": "linear"}
```

### Status: PASS

---

## 3. Webhook Signature Verification

### 3.1 Valid Signature (via Linear)

**Test:** Trigger a real webhook from Linear (see Section 4). Check server logs for successful processing.

**Expected:** HTTP 200 returned, no "Invalid webhook signature" warning in logs.

**Status:** PASS

### 3.2 Invalid Signature

**Test:**

```bash
curl -X POST http://localhost:8000/webhooks/linear \
  -H "Content-Type: application/json" \
  -H "Linear-Signature: deadbeef" \
  -d '{"type": "AgentSessionEvent", "action": "created"}'
```

**Expected:** HTTP 400, log message "Invalid webhook signature".

**Status:** PASS

### 3.3 Expired Timestamp

**Test:**

```bash
curl -X POST http://localhost:8000/webhooks/linear \
  -H "Content-Type: application/json" \
  -H "Linear-Signature: (compute valid sig)" \
  -d '{"type": "AgentSessionEvent", "action": "created", "webhookTimestamp": 1000000}'
```

**Expected:** HTTP 400, log message "Webhook timestamp too old".

**Status:** PASS

---

## 4. New Session (End-to-End)

This is the primary happy path. It verifies the full flow from webhook to agent execution to Linear activity updates.

### Test

1. In Linear, create an issue on a team that is mapped in `PROJECT_MAPPINGS_JSON`
2. @mention your agent app in a comment on the issue, or delegate the issue to it
3. Linear sends a `created` webhook to your bridge

### Expected (check in order)

1. **Webhook received** — Server logs show the incoming payload
2. **Immediate acknowledgment (<10s)** — A `thought` activity appears in Linear's agent session panel ("Starting work...")
3. **Issue state change** — The issue moves to the first "started" workflow state
4. **Agent thoughts** — Ephemeral `thought` activities appear as the agent reasons
5. **Tool calls** — `action` activities show tool invocations (file reads, edits, terminal commands)
6. **Plan updates** — If the agent creates a plan, it appears as a checklist in the session
7. **Final response** — A `response` activity with the agent's summary of work done
8. **Session state** — The Linear agent session transitions to `complete`

### Timing Expectations

| Event | Deadline |
|-------|----------|
| HTTP 200 to webhook | < 5 seconds |
| First `thought` activity in Linear | < 10 seconds |
| Subsequent activities | Every < 30 minutes |

### Status: PARTIAL PASS

- Webhook reception, acknowledgment, issue state change, and agent startup all work
- Agent authenticates and begins processing
- Activity updates (thoughts, actions, responses) are sent to Linear
- Still testing full end-to-end completion

---

## 5. Follow-Up Message

### Test

1. After the initial session completes (Section 4), send a follow-up message in the same Linear agent session
2. Linear sends a `prompted` webhook

### Expected

1. A new `thought` activity appears ("Processing follow-up...")
2. The agent processes the follow-up message
3. A `response` activity appears with the result
4. The session returns to `complete`

### Status: NOT YET TESTED (end-to-end)

Follow-up prompt extraction is now working (fixed `content.body` parsing — see Bugs
Found below), but has not been tested through a full cycle yet.

**Known limitation:** Session state is in-memory only. If the container restarts
between the initial session and the follow-up, the follow-up will fail with
"No active session for follow-up". This is by design (see Section 9).

---

## 6. Stop Signal

### Test

1. While the agent is actively working on a session, click the stop button in Linear's agent session UI
2. Linear sends a `prompted` webhook with `agentActivity.signal = "stop"`

### Expected

1. The agent session is cancelled
2. A `response` activity appears: "Stopped as requested."
3. The ACP subprocess terminates

### Status: PASS

Stop signal is correctly detected from the webhook payload and the session is
cancelled with a response sent to Linear.

---

## 7. Error Handling

### 7.1 Invalid ACP Command

**Test:** Set `ACP_AGENT_COMMAND=nonexistent-binary` in `.env`, restart, and trigger a webhook.

**Expected:** An `error` activity appears in Linear: "Failed to start agent session".

### 7.2 Missing Project Mapping

**Test:** Trigger a webhook from a team ID that is not in `PROJECT_MAPPINGS_JSON`.

**Expected:** The agent starts in the fallback directory (`/data/projects`). Check logs for the resolved cwd.

### 7.3 Empty Prompt Context

**Test:** Trigger a session where `promptContext` is empty/null (e.g., minimal issue with no description).

**Expected:** The agent receives a fallback prompt with the issue title/identifier. Check that it still processes.

### 7.4 Agent Execution Error

**Test:** Point the agent at a project directory that doesn't exist or has no permissions.

**Expected:** An `error` activity appears in Linear: "Agent encountered an error during execution".

### Status: NOT YET TESTED

---

## 8. Concurrent Sessions

### Test

1. Trigger two new sessions simultaneously (e.g., delegate two issues to the agent at the same time)

### Expected

1. Both sessions process independently
2. Each gets its own thought/action/response activities
3. No cross-contamination between sessions
4. Server logs show both session IDs interleaved

### Status: NOT YET TESTED

---

## 9. Shutdown Behavior

### Test

1. While an agent session is actively running, stop the Docker container:

```bash
docker compose down
```

### Expected

1. Server logs show "Shutting down ACP Bridge..."
2. All active ACP subprocesses are terminated
3. Container exits cleanly

### Known Limitation

Session state is stored in-memory only (`SessionManager._active_sessions`). All
session state is lost on container restart. Follow-up messages to sessions that
existed before a restart will fail with "No active session for follow-up".

### Status: NOT YET TESTED

---

## 10. Log Monitoring

Throughout all tests, monitor the server logs for:

```bash
docker compose logs -f
```

### What to look for

- `Registered adapter: linear` — Adapter loaded on startup
- `ACP Bridge started (services: linear)` — App ready
- `Webhook received: type=... action=...` — Incoming webhook
- `Raw prompted payload: ...` — Full payload for prompted webhooks (for debugging)
- `ACP session started: <id> (cwd=<path>)` — Agent subprocess spawned
- `ACP session stopped` — Agent subprocess cleaned up
- `Prompted webhook for <id>: agentActivity=...` — Follow-up payload details
- `Error in session update callback` — Update routing failures
- `GraphQL errors: [...]` — Linear API call failures
- `Invalid webhook signature` — Auth failures
- `Webhook timestamp too old` — Replay protection triggers
- `No active session for follow-up` — Follow-up on a lost session (restart?)

---

## Bugs Found and Fixed During Testing

### 1. Wrong npm package name in Dockerfile

**Symptom:** `npm install -g @anthropic-ai/claude-code-acp` returned 404.

**Fix:** Changed to `npm install -g @zed-industries/claude-code-acp`. The binary
name is still `claude-code-acp`.

### 2. Python version too old in Dockerfile

**Symptom:** `pip install` failed with "requires Python >=3.12" but Debian bookworm
ships Python 3.11.

**Fix:** Changed base image from `debian:bookworm-slim` to
`python:3.12-slim-bookworm`.

### 3. Follow-up prompt not extracted from webhook payload

**Symptom:** Follow-up webhooks logged "Empty prompt" and were silently dropped.

**Root cause:** The code read `agentActivity.body` which is `null`. The actual user
message is in `agentActivity.content.body` (a nested object).

**Fix:** Added `LinearAgentActivityContent` model and updated `_handle_prompted` to
read from `content.body`, falling back to `body`, then `promptContext`.

### 4. Linear API rejects action activities without `parameter` field

**Symptom:** Repeated GraphQL errors: "Action activities must include a 'parameter'
field."

**Fix:** Always include `parameter` in action activity content, defaulting to
empty string when not provided.

### 5. asyncio StreamReader buffer overflow on large ACP messages

**Symptom:** `ValueError: Separator is found, but chunk is longer than limit` —
the agent subprocess sent a JSON-RPC message exceeding the default 64KB buffer.

**Fix:** Added `limit=10 * 1024 * 1024` (10MB) to `asyncio.create_subprocess_exec`
in `app/acp/session.py`.

### 6. `.env.example` had wrong variable name

**Symptom:** `.env.example` used `PROJECT_MAPPINGS` but the code reads
`PROJECT_MAPPINGS_JSON`.

**Fix:** Updated `.env.example` to use the correct name.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| No webhook received | Tunnel not running, wrong URL in Linear | Check tunnel, verify webhook URL |
| 400 on webhook | Wrong `LINEAR_WEBHOOK_SECRET` | Verify secret matches Linear app settings |
| "Failed to start agent session" | `claude-code-acp` not in PATH | Check Dockerfile installed it, verify `ACP_AGENT_COMMAND` |
| 401 "Invalid bearer token" | Wrong/expired auth token | Verify `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` |
| 401 after setting OAuth token | `#` in token truncated by `.env` parser | Wrap the value in double quotes |
| No activities in Linear | Wrong `LINEAR_ACCESS_TOKEN` | Verify token has correct scopes |
| Agent starts but does nothing useful | Wrong `PROJECT_MAPPINGS_JSON` | Verify team ID and directory path |
| "Empty prompt in prompted webhook" | Prompt in wrong payload field | Check `agentActivity.content.body` is being parsed |
| "No active session for follow-up" | Container restarted between sessions | Session state is in-memory only — expected after restart |
| "Action activities must include parameter" | Missing `parameter` in action content | Ensure `parameter` is always set (even to empty string) |
| "chunk is longer than limit" | ACP message exceeds asyncio buffer | Increase `limit` in `create_subprocess_exec` |
| Session goes stale (>30 min) | Agent hung or debouncing too aggressively | Check ACP subprocess health, review logs |
| "GraphQL errors" in logs | Token expired or invalid mutation | Refresh OAuth token, check Linear API docs |
| `.env` changes not taking effect | Used `docker compose restart` | Must use `docker compose down && docker compose up` |
