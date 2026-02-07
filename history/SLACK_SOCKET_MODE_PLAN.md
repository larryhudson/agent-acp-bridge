# Slack Socket Mode Integration Plan

## Overview

This plan details how to implement a Slack adapter for the ACP bridge using Slack's **Socket Mode**, which differs significantly from Linear's webhook-based approach. Socket Mode uses a persistent WebSocket connection instead of HTTP webhooks, eliminating the need for a publicly-exposed endpoint.

## Key Differences from Linear Integration

| Aspect | Linear (Webhooks) | Slack (Socket Mode) |
|--------|------------------|---------------------|
| **Connection** | HTTP POST to public endpoint | Persistent WebSocket connection |
| **Authentication** | HMAC signature verification | App-level token (`xapp-`) + bot token |
| **Event delivery** | Slack pushes to our server | We connect to Slack and receive events |
| **Acknowledgment** | HTTP 200 response | Send envelope_id back over WebSocket |
| **Lifecycle** | Stateless (per request) | Stateful (connection management, reconnects) |
| **Progress updates** | Create activity for each update | Edit a single message to show progress |
| **Infrastructure** | Requires public URL | Works behind firewall |

## How Slack Socket Mode Works

1. **Initialization**: App calls `apps.connections.open` with app-level token to get a WebSocket URL
2. **Connection**: App opens WebSocket connection to the dynamic URL
3. **Hello message**: Slack sends `hello` event with connection metadata
4. **Event flow**:
   - Slack sends events wrapped in envelopes with unique `envelope_id`
   - App processes event
   - App **must** acknowledge by sending back `{"envelope_id": "...", "payload": {}}`
5. **Reconnection**: Connection refreshes every few hours; app must reconnect
6. **Graceful shutdown**: Slack sends warning 10 seconds before disconnect

## Architecture Changes

### New Components

```
app/services/slack/
  __init__.py
  adapter.py           # SlackAdapter (implements ServiceAdapter)
  socket_client.py     # WebSocket connection manager
  api_client.py        # Slack Web API client (chat.postMessage, chat.update)
  models.py            # Pydantic models for Slack events
  events.py            # Event payload parsing
```

### Socket Client Lifecycle

The `SlackSocketClient` will manage the WebSocket connection:

- **Background task**: Runs as a FastAPI background task, separate from HTTP request handlers
- **Connection initialization**: Calls `apps.connections.open` to get WebSocket URL
- **Event loop**: Listens for events, acknowledges them, and routes to the adapter
- **Auto-reconnect**: Handles disconnect events and reconnection logic
- **Graceful shutdown**: Closes connection cleanly on app shutdown

### Integration with FastAPI

Unlike Linear's webhook routes, Slack Socket Mode requires a **persistent background task**.

With the updated `ServiceAdapter` protocol, this is handled cleanly:

```python
# In app/main.py lifespan (already implemented):
for adapter in _adapters:
    adapter.register_routes(app)  # No-op for Slack
    await adapter.start()         # Launches WebSocket connection
    logger.info("Started adapter: %s", adapter.service_name)

# ... app runs ...

# Shutdown:
for adapter in _adapters:
    await adapter.close()  # Closes WebSocket connection
```

The `SlackAdapter.start()` method launches the WebSocket client as a background task.

## Event Flow

### 1. App Mention Event

When a user @mentions the bot in a channel or thread:

```
User posts: "@agent please help with this issue"
  ↓
Slack sends event via WebSocket:
{
  "envelope_id": "abc123",
  "type": "events_api",
  "payload": {
    "event": {
      "type": "app_mention",
      "user": "U123",
      "text": "<@UBOT> please help with this issue",
      "channel": "C123",
      "ts": "1234567890.123456",
      "thread_ts": null  // or timestamp if in thread
    }
  }
}
  ↓
SlackSocketClient acknowledges: {"envelope_id": "abc123", "payload": {}}
  ↓
SlackAdapter.on_session_created() creates BridgeSessionRequest:
  - external_session_id: f"slack:{channel}:{thread_ts or ts}"
  - prompt: "please help with this issue" (strip bot mention)
  - cwd: from project_mappings["slack:C123"]
  ↓
SessionManager.handle_new_session(adapter, request)
  ↓
SlackAdapter posts initial message:
  "🤔 Thinking..." (stores message ts for updates)
  ↓
Agent produces updates → SlackAdapter.send_update()
  - "thought": Edit message to show current thought
  - "tool_call": Edit message to add tool execution status
  - "message_chunk": Accumulate for final message
  ↓
Agent completes → SlackAdapter.send_completion()
  - Edit message with final response
  - Add ✅ reaction to original message
```

### 2. Thread Reply (Follow-up)

When user replies in the thread:

```
User posts in thread: "Can you also check X?"
  ↓
Slack sends message event (thread_ts matches original)
  ↓
SlackAdapter recognizes existing session
  ↓
SessionManager.handle_followup(session_id, new_prompt)
  ↓
Agent continues in same session
  ↓
Post new reply in thread with response
```

## Update Translation Strategy

Unlike Linear's activity stream, Slack uses **message editing** to show progress:

```python
async def send_update(self, session_id: str, update: BridgeUpdate) -> None:
    session_data = self._sessions.get(session_id)
    if not session_data:
        return

    channel = session_data["channel"]
    progress_message_ts = session_data["progress_message_ts"]

    if update.type == "thought":
        # Edit the progress message
        new_text = f"💭 {update.content}"
        await self._api.update_message(channel, progress_message_ts, new_text)

    elif update.type == "tool_call":
        # Show tool execution
        current_text = session_data.get("current_text", "")
        new_text = f"{current_text}\n⚙️ {update.content}"
        await self._api.update_message(channel, progress_message_ts, new_text)

    elif update.type == "message_chunk":
        # Accumulate for final response
        self._message_buffers.setdefault(session_id, "")
        self._message_buffers[session_id] += update.content

    elif update.type == "plan":
        # Format as a structured plan
        plan_text = "📋 Plan:\n"
        for step in update.metadata.get("entries", []):
            status = {"pending": "⏳", "in_progress": "▶️", "completed": "✅"}
            icon = status.get(step["status"], "⏳")
            plan_text += f"{icon} {step['content']}\n"
        await self._api.update_message(channel, progress_message_ts, plan_text)
```

## Session Management

### Session ID Format

Use composite key to track Slack conversations:
```
slack:{channel_id}:{thread_ts}
```

Examples:
- New conversation: `slack:C123456:1234567890.123456`
- Thread reply: `slack:C123456:1234567890.123456` (same as parent)

### Session Data Structure

```python
{
    "slack:C123456:1234567890.123456": {
        "channel": "C123456",
        "thread_ts": "1234567890.123456",  # Thread parent timestamp
        "progress_message_ts": "1234567890.999999",  # The message we're editing
        "current_text": "💭 Analyzing the codebase...",
    }
}
```

## Configuration

### Environment Variables

Add to `.env`:

```bash
# Slack
SLACK_APP_TOKEN=xapp-1-xxxxx           # App-level token for Socket Mode
SLACK_BOT_TOKEN=xoxb-xxxxx             # Bot token for Web API calls
SLACK_ENABLED=true                      # Enable/disable Slack adapter

# Project mappings
PROJECT_MAPPINGS={
  "linear:team_xxx": "/data/projects/my-app",
  "slack:C123456": "/data/projects/my-app",      # Map by channel
  "slack:C789012": "/data/projects/other-app"
}
```

### Slack App Setup

1. **Create Slack App** at https://api.slack.com/apps
2. **Enable Socket Mode**:
   - Settings → Socket Mode → Enable
   - Generate app-level token with `connections:write` scope
3. **Add Bot Scopes**:
   - `app_mentions:read` — receive @mentions
   - `channels:history` — read channel messages
   - `channels:read` — view channel info
   - `chat:write` — send messages
   - `groups:history` — read private channel messages (optional)
   - `im:history` — read DMs (optional)
4. **Subscribe to Events**:
   - `app_mention` — when bot is @mentioned
   - `message.channels` — messages in channels (optional, for non-mention triggers)
   - `message.groups` — messages in private channels (optional)
5. **Install App** to workspace → get bot token

## Implementation Steps

### Step 0: Refactor ServiceAdapter Protocol ✅ COMPLETED

**Goal**: Extend the `ServiceAdapter` protocol to support both webhook-based and socket-based adapters.

**Changes made**:

1. **Updated `app/core/types.py`**:
   - Added `async def start()` method for background task initialization
   - Added `async def close()` method for resource cleanup
   - Documented which methods are optional vs required
   - Clarified that webhook vs socket adapters use different subsets of methods

2. **Updated `app/services/linear/adapter.py`**:
   - Added `async def start()` as no-op (webhook adapters don't need background tasks)
   - `close()` method already existed

3. **Updated `app/main.py`**:
   - Call `await adapter.start()` for each adapter during lifespan startup
   - Call `await adapter.close()` during shutdown (removed `hasattr` check since it's now in protocol)

**Result**: The architecture now cleanly supports both connection models without hacky `hasattr()` checks or `NotImplementedError` workarounds.

---

### Step 1: Add Dependencies

Update `pyproject.toml`:
```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.34.0",
    "httpx>=0.28.0",
    "agent-client-protocol>=0.1.0",
    "pydantic-settings>=2.7.0",
    "websockets>=14.0",        # NEW: WebSocket client
    "slack-sdk>=3.36.0",       # NEW: Official Slack SDK
]
```

Run `uv sync` to install.

### Step 2: Slack Models (`app/services/slack/models.py`)

Define Pydantic models for Slack events:

```python
from pydantic import BaseModel, Field

class SlackEvent(BaseModel):
    """Base Slack event."""
    type: str
    user: str | None = None
    channel: str | None = None
    ts: str | None = None

class AppMentionEvent(SlackEvent):
    """app_mention event."""
    type: Literal["app_mention"]
    user: str
    text: str
    channel: str
    ts: str
    thread_ts: str | None = None

class MessageEvent(SlackEvent):
    """message event."""
    type: Literal["message"]
    user: str | None = None
    text: str | None = None
    channel: str
    ts: str
    thread_ts: str | None = None
    subtype: str | None = None  # Ignore bot messages

class EventEnvelope(BaseModel):
    """Slack event envelope."""
    envelope_id: str
    type: str
    payload: dict[str, Any]
```

### Step 3: Slack API Client (`app/services/slack/api_client.py`)

Wrapper for Slack Web API using `httpx`:

```python
import httpx
from typing import Any

class SlackApiClient:
    """Async Slack Web API client."""

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token
        self._client = httpx.AsyncClient(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {bot_token}"},
        )

    async def post_message(
        self, channel: str, text: str, thread_ts: str | None = None
    ) -> dict[str, Any]:
        """Post a message to a channel or thread."""
        response = await self._client.post(
            "/chat.postMessage",
            json={
                "channel": channel,
                "text": text,
                "thread_ts": thread_ts,
            },
        )
        response.raise_for_status()
        return response.json()

    async def update_message(
        self, channel: str, ts: str, text: str
    ) -> dict[str, Any]:
        """Update an existing message."""
        response = await self._client.post(
            "/chat.update",
            json={
                "channel": channel,
                "ts": ts,
                "text": text,
            },
        )
        response.raise_for_status()
        return response.json()

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        """Add a reaction to a message."""
        await self._client.post(
            "/reactions.add",
            json={
                "channel": channel,
                "timestamp": ts,
                "name": emoji,
            },
        )

    async def get_websocket_url(self, app_token: str) -> str:
        """Get WebSocket URL from apps.connections.open."""
        response = await self._client.post(
            "/apps.connections.open",
            headers={"Authorization": f"Bearer {app_token}"},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Failed to get WebSocket URL: {data}")
        return data["url"]

    async def close(self) -> None:
        await self._client.aclose()
```

### Step 4: Socket Client (`app/services/slack/socket_client.py`)

Manages the WebSocket connection and event loop:

```python
import asyncio
import json
import logging
from typing import Callable, Any

import websockets
from websockets.client import WebSocketClientProtocol

from app.services.slack.api_client import SlackApiClient
from app.services.slack.models import EventEnvelope

logger = logging.getLogger(__name__)

class SlackSocketClient:
    """Manages Slack Socket Mode WebSocket connection."""

    def __init__(
        self,
        app_token: str,
        bot_token: str,
        on_event: Callable[[EventEnvelope], Any],
    ) -> None:
        self._app_token = app_token
        self._api = SlackApiClient(bot_token)
        self._on_event = on_event
        self._ws: WebSocketClientProtocol | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the WebSocket client as a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the WebSocket client."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            await self._task

    async def _run(self) -> None:
        """Main connection loop with auto-reconnect."""
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception:
                logger.exception("Socket Mode connection error, reconnecting in 5s")
                await asyncio.sleep(5)

    async def _connect_and_listen(self) -> None:
        """Connect to WebSocket and listen for events."""
        # Get WebSocket URL
        ws_url = await self._api.get_websocket_url(self._app_token)
        logger.info("Connecting to Slack Socket Mode: %s", ws_url[:50])

        async with websockets.connect(ws_url) as ws:
            self._ws = ws
            logger.info("Connected to Slack Socket Mode")

            async for message in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except Exception:
                    logger.exception("Error handling message: %s", message)

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Handle incoming WebSocket message."""
        msg_type = data.get("type")

        if msg_type == "hello":
            logger.info("Received hello: %s", data)
            return

        if msg_type == "disconnect":
            logger.warning("Disconnect requested: %s", data.get("reason"))
            return

        # Event envelope — must acknowledge
        if "envelope_id" in data:
            envelope = EventEnvelope.model_validate(data)

            # Acknowledge immediately
            await self._acknowledge(envelope.envelope_id)

            # Process event asynchronously
            asyncio.create_task(self._on_event(envelope))

    async def _acknowledge(self, envelope_id: str) -> None:
        """Acknowledge an event envelope."""
        if not self._ws:
            return

        ack = json.dumps({"envelope_id": envelope_id, "payload": {}})
        await self._ws.send(ack)
```

### Step 5: Slack Adapter (`app/services/slack/adapter.py`)

Implements `ServiceAdapter` protocol:

```python
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import FastAPI

from app.config import settings
from app.core.types import BridgeSessionRequest, BridgeUpdate
from app.services.slack.api_client import SlackApiClient
from app.services.slack.models import AppMentionEvent, EventEnvelope
from app.services.slack.socket_client import SlackSocketClient

logger = logging.getLogger(__name__)

class SlackAdapter:
    """Service adapter for Slack using Socket Mode."""

    service_name: str = "slack"

    def __init__(self, session_manager: Any) -> None:
        self._session_manager = session_manager
        self._api = SlackApiClient(settings.slack_bot_token)
        self._socket_client = SlackSocketClient(
            app_token=settings.slack_app_token,
            bot_token=settings.slack_bot_token,
            on_event=self._handle_event,
        )
        self._sessions: dict[str, dict[str, Any]] = {}
        self._message_buffers: dict[str, str] = {}

    async def start(self) -> None:
        """Start the Socket Mode client (implements ServiceAdapter.start)."""
        self._socket_client.start()

    def register_routes(self, app: FastAPI) -> None:
        """No routes needed for Socket Mode (implements ServiceAdapter.register_routes)."""
        pass

    async def _handle_event(self, envelope: EventEnvelope) -> None:
        """Handle an event from Socket Mode."""
        if envelope.type != "events_api":
            return

        event_data = envelope.payload.get("event", {})
        event_type = event_data.get("type")

        if event_type == "app_mention":
            await self._handle_app_mention(event_data)
        elif event_type == "message":
            await self._handle_message(event_data)

    async def _handle_app_mention(self, event: dict[str, Any]) -> None:
        """Handle app_mention event (new session)."""
        mention_event = AppMentionEvent.model_validate(event)

        # Create session ID
        thread_ts = mention_event.thread_ts or mention_event.ts
        session_id = f"slack:{mention_event.channel}:{thread_ts}"

        # Extract prompt (strip bot mention)
        prompt = re.sub(r"<@\w+>\s*", "", mention_event.text).strip()

        # Determine working directory
        cwd = settings.get_cwd_for_key(f"slack:{mention_event.channel}")
        if not cwd:
            # Fallback: first slack mapping
            for key, path in settings.project_mappings.items():
                if key.startswith("slack:"):
                    cwd = path
                    break
            else:
                cwd = "/data/projects"

        # Post initial "thinking" message
        response = await self._api.post_message(
            channel=mention_event.channel,
            text="🤔 Thinking...",
            thread_ts=thread_ts,
        )
        progress_ts = response["ts"]

        # Track session
        self._sessions[session_id] = {
            "channel": mention_event.channel,
            "thread_ts": thread_ts,
            "progress_message_ts": progress_ts,
            "current_text": "🤔 Thinking...",
        }

        # Create bridge request
        request = BridgeSessionRequest(
            external_session_id=session_id,
            service_name=self.service_name,
            prompt=prompt,
            cwd=cwd,
        )

        await self._session_manager.handle_new_session(self, request)

    async def _handle_message(self, event: dict[str, Any]) -> None:
        """Handle message event (follow-up in thread)."""
        # Ignore bot messages
        if event.get("subtype") == "bot_message" or not event.get("user"):
            return

        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return  # Not in a thread

        session_id = f"slack:{event['channel']}:{thread_ts}"
        if session_id not in self._sessions:
            return  # No active session

        # Follow-up prompt
        prompt = event.get("text", "").strip()
        if not prompt:
            return

        await self._session_manager.handle_followup(session_id, prompt)

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        """Not used — events handled via Socket Mode."""
        raise NotImplementedError

    async def send_update(self, session_id: str, update: BridgeUpdate) -> None:
        """Translate BridgeUpdate to Slack message edits."""
        session_data = self._sessions.get(session_id)
        if not session_data:
            return

        try:
            channel = session_data["channel"]
            ts = session_data["progress_message_ts"]

            if update.type == "thought":
                new_text = f"💭 {update.content}"
                await self._api.update_message(channel, ts, new_text)
                session_data["current_text"] = new_text

            elif update.type == "tool_call":
                current = session_data.get("current_text", "")
                new_text = f"{current}\n⚙️ `{update.content}`"
                await self._api.update_message(channel, ts, new_text)
                session_data["current_text"] = new_text

            elif update.type == "message_chunk":
                self._message_buffers.setdefault(session_id, "")
                self._message_buffers[session_id] += update.content

            elif update.type == "plan":
                entries = update.metadata.get("entries", [])
                plan_text = "📋 *Plan:*\n"
                status_icons = {"pending": "⏳", "in_progress": "▶️", "completed": "✅"}
                for entry in entries:
                    icon = status_icons.get(entry.get("status", "pending"), "⏳")
                    plan_text += f"{icon} {entry['content']}\n"
                await self._api.update_message(channel, ts, plan_text)
                session_data["current_text"] = plan_text

        except Exception:
            logger.exception("Error sending update to Slack for %s", session_id)

    async def send_completion(self, session_id: str, message: str) -> None:
        """Send completion message."""
        session_data = self._sessions.pop(session_id, None)
        if not session_data:
            return

        final_text = self._message_buffers.pop(session_id, "") or message

        try:
            channel = session_data["channel"]
            ts = session_data["progress_message_ts"]

            # Update progress message with final response
            await self._api.update_message(channel, ts, final_text)

            # Add checkmark reaction to original mention
            thread_ts = session_data["thread_ts"]
            await self._api.add_reaction(channel, thread_ts, "white_check_mark")

        except Exception:
            logger.exception("Error sending completion to Slack for %s", session_id)

    async def send_error(self, session_id: str, error: str) -> None:
        """Send error message."""
        session_data = self._sessions.pop(session_id, None)
        self._message_buffers.pop(session_id, None)

        if not session_data:
            return

        try:
            channel = session_data["channel"]
            ts = session_data["progress_message_ts"]
            await self._api.update_message(channel, ts, f"❌ Error: {error}")
        except Exception:
            logger.exception("Error sending error to Slack for %s", session_id)

    async def close(self) -> None:
        """Clean up resources."""
        await self._socket_client.stop()
        await self._api.close()
```

### Step 6: Config Updates (`app/config.py`)

Add Slack settings:

```python
class Settings(BaseSettings):
    # ... existing fields ...

    # Slack
    slack_app_token: str = ""
    slack_bot_token: str = ""
    slack_enabled: bool = False
```

### Step 7: Main App Integration ✅ ALREADY DONE

The main app integration is already handled by the refactored protocol in Step 0.

When you add Slack to `ENABLED_SERVICES`, the app will automatically:
1. Instantiate `SlackAdapter` in `_create_adapters()`
2. Call `adapter.register_routes(app)` (no-op for Slack)
3. Call `await adapter.start()` (launches WebSocket connection)
4. Call `await adapter.close()` on shutdown (closes connection)

**What you need to do**:
- Add Slack adapter instantiation to `_create_adapters()` in `app/main.py`:

```python
def _create_adapters(session_manager: SessionManager) -> list[ServiceAdapter]:
    """Instantiate adapters for all enabled services."""
    adapters: list[ServiceAdapter] = []

    for service in settings.enabled_services_list:
        if service == "linear":
            from app.services.linear.adapter import LinearAdapter
            adapters.append(LinearAdapter(session_manager))
        elif service == "slack":  # NEW
            from app.services.slack.adapter import SlackAdapter
            adapters.append(SlackAdapter(session_manager))
        else:
            logger.warning("Unknown service: %s (skipping)", service)

    return adapters
```

## Testing Strategy

### Unit Tests

1. **Socket client mock**: Test event handling and acknowledgment
2. **Adapter event parsing**: Verify BridgeSessionRequest creation
3. **Update translation**: Verify message editing logic

### Integration Tests

1. **Mock WebSocket**: Simulate Socket Mode events
2. **Mock Slack API**: Verify API calls for message posting/updating
3. **Session lifecycle**: Test new session → updates → completion flow

### Manual E2E Testing

1. **Setup**:
   - Create Slack app with Socket Mode enabled
   - Configure tokens in `.env`
   - Add bot to test channel
   - Run bridge: `docker compose up`

2. **Test scenarios**:
   - **New conversation**: @mention bot → verify thinking message → see updates → final response
   - **Thread reply**: Reply in thread → verify follow-up works
   - **Stop signal**: (Slack doesn't have built-in stop, but test error handling)
   - **Reconnection**: Kill WebSocket → verify auto-reconnect

## Migration Path

1. ✅ **Step 0**: Refactor ServiceAdapter protocol (COMPLETED)
2. 📝 **Step 1**: Add dependencies (websockets, slack-sdk)
3. 📝 **Step 2**: Slack models (EventEnvelope, AppMentionEvent, etc.)
4. 📝 **Step 3**: Slack API client (post/update messages, WebSocket URL)
5. 📝 **Step 4**: Socket client with connection management
6. 📝 **Step 5**: Adapter implementation
7. 📝 **Step 6**: Config updates (SLACK_APP_TOKEN, SLACK_BOT_TOKEN)
8. 📝 **Step 7**: Main app integration (add to _create_adapters)
9. 🧪 **Testing**: Unit + integration tests
10. 🚀 **Deploy**: Test in staging, then production

## Considerations & Limitations

### Slack API Limits

- **Message updates**: No hard limit, but Slack recommends <1/second per message
- **Rate limits**: Tier 1 (50+ requests/min), Tier 2 (20+ requests/min), Tier 3 (1+ requests/min)
- **WebSocket**: Max 10 concurrent connections per app

### State Management

- **Session persistence**: Currently in-memory. If bridge restarts, active sessions are lost.
  - **Future improvement**: Use Redis or database to persist session state

### Error Handling

- **WebSocket disconnect**: Auto-reconnect with exponential backoff
- **API errors**: Log and continue (don't crash the entire bridge)
- **Event processing errors**: Acknowledge event even if processing fails (avoid retries)

### Security

- **Token storage**: Use environment variables, never commit to git
- **Validation**: No signature verification needed (Socket Mode is pre-authenticated)

## Future Enhancements

1. **Rich message formatting**: Use Slack Block Kit for structured updates
2. **Interactive components**: Add buttons for "Stop", "Retry", etc.
3. **DM support**: Handle direct messages to bot
4. **Multi-workspace**: Support multiple Slack workspaces with different tokens
5. **Persistence**: Store session state in Redis for resilience across restarts

## Reference Documentation

- [Slack Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/)
- [Slack Events API](https://docs.slack.dev/apis/events-api/)
- [Slack Web API - chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage)
- [Slack Web API - chat.update](https://docs.slack.dev/reference/methods/chat.update)
- [Slack Bolt for Python - Socket Mode](https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/)
- [Slack Python SDK - Socket Mode Client](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/)

## Comparison: Custom Implementation vs. Slack Bolt

This plan uses a **custom Socket Mode implementation** rather than the Slack Bolt framework. Here's why:

| Approach | Pros | Cons |
|----------|------|------|
| **Custom** (this plan) | - Full control over event handling<br>- Minimal dependencies<br>- Fits cleanly into ServiceAdapter pattern<br>- No framework lock-in | - More boilerplate code<br>- Manual WebSocket management |
| **Slack Bolt** | - Less boilerplate<br>- Built-in Socket Mode handler<br>- More examples/docs | - Framework-specific patterns<br>- Harder to fit into ServiceAdapter abstraction<br>- Additional dependency |

**Recommendation**: Start with custom implementation for cleaner architecture. If complexity grows, consider migrating to Bolt.

---

## Next Steps

1. Review this plan with stakeholders
2. Get Slack app credentials (app token, bot token)
3. Implement Step 1 (dependencies)
4. Proceed through steps 2-7 incrementally
5. Test thoroughly before production deployment
