"""SlackAdapter — implements ServiceAdapter for Slack's Socket Mode."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import FastAPI

from app.config import settings
from app.core.repo_provider import slugify
from app.core.types import BridgeSessionRequest, BridgeUpdate
from app.services.slack.api_client import SlackApiClient
from app.services.slack.models import AppMentionEvent, EventEnvelope, MessageEvent
from app.services.slack.socket_client import SlackSocketClient

logger = logging.getLogger(__name__)

# Slack's message limit is ~40k characters; use a conservative threshold.
SLACK_MAX_MESSAGE_LENGTH = 30_000
SLACK_TRUNCATION_NOTICE = "\n\n_(message truncated — too long for Slack)_"
SLACK_RETRY_MAX_MESSAGE_LENGTH = 10_000


def _truncate_for_slack(text: str, max_length: int = SLACK_MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= max_length:
        return text

    max_len = max_length - len(SLACK_TRUNCATION_NOTICE)
    if max_len <= 0:
        return SLACK_TRUNCATION_NOTICE[:max_length]

    return text[:max_len] + SLACK_TRUNCATION_NOTICE


class SlackAdapter:
    """Service adapter for Slack using Socket Mode.

    Handles:
    - WebSocket connection management
    - Event routing (app_mention, message)
    - Translating BridgeUpdates to Slack message edits
    - Session lifecycle management
    """

    def __init__(
        self,
        session_manager: Any,
        agent_name: str,
        bot_token: str,
        app_token: str,
    ) -> None:
        self._session_manager = session_manager
        self._agent_name = agent_name
        # Unique service_name per agent for session tracking
        self.service_name: str = f"slack:{agent_name}" if agent_name else "slack"
        self._api = SlackApiClient(bot_token)
        self._socket_client = SlackSocketClient(
            app_token=app_token,
            bot_token=bot_token,
            on_event=self._handle_event,
        )
        # Bot's own Slack user ID (fetched at startup via auth.test)
        self._bot_user_id: str | None = None
        # Track session data: {session_id: {channel, thread_ts, progress_message_ts, ...}}
        self._sessions: dict[str, dict[str, Any]] = {}
        # Accumulate message chunks for final response
        self._message_buffers: dict[str, str] = {}
        # Track threads where bot has been mentioned (persists after session completion)
        # Format: {(channel, thread_ts)}
        self._active_threads: set[tuple[str, str]] = set()
        # Cache user display names to avoid repeated API calls
        self._user_name_cache: dict[str, str] = {}

    async def _safe_update_message(self, channel: str, ts: str, text: str) -> None:
        try:
            await self._api.update_message(channel, ts, text)
        except RuntimeError as exc:
            if "msg_too_long" not in str(exc):
                raise

            logger.warning("Slack msg_too_long for %s:%s; retrying with shorter text", channel, ts)
            truncated = _truncate_for_slack(text, max_length=SLACK_RETRY_MAX_MESSAGE_LENGTH)
            await self._api.update_message(channel, ts, truncated)

    def register_routes(self, app: FastAPI) -> None:
        """No routes needed for Socket Mode (implements ServiceAdapter.register_routes)."""
        pass

    async def start(self) -> None:
        """Start the Socket Mode client and detect bot user ID."""
        try:
            auth_info = await self._api.auth_test()
            self._bot_user_id = auth_info.get("user_id")
            logger.info(
                "Slack bot identity: user_id=%s, user=%s",
                self._bot_user_id,
                auth_info.get("user"),
            )
        except Exception:
            logger.exception("Failed to get bot identity via auth.test")

        self._socket_client.start()
        logger.info("Slack Socket Mode client started")

    def restore_persisted_sessions(self) -> None:
        """Rebuild adapter state from sessions restored by the session manager."""
        restored = self._session_manager.get_sessions_for_service(self.service_name)
        for session_id, active in restored.items():
            if active.service_metadata:
                self._sessions[session_id] = active.service_metadata
                channel = active.service_metadata.get("channel")
                thread_ts = active.service_metadata.get("thread_ts")
                if channel and thread_ts:
                    self._active_threads.add((channel, thread_ts))
                logger.debug(
                    "Restored session %s with metadata: %s", session_id, active.service_metadata
                )
        if restored:
            logger.info("Restored %d Slack session(s) from persistence", len(restored))
            logger.info("Active sessions after restore: %s", list(self._sessions.keys()))

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        """Not used — events handled via Socket Mode (implements ServiceAdapter.on_session_created)."""
        raise NotImplementedError("Socket Mode adapters handle events via WebSocket callbacks")

    async def _handle_event(self, envelope: EventEnvelope) -> None:
        """Handle an event from Socket Mode.

        Args:
            envelope: Event envelope from WebSocket
        """
        if envelope.type != "events_api":
            return

        event_data = envelope.payload.get("event", {})
        event_type = event_data.get("type")

        # DEBUG: Log raw event payload
        logger.info("Raw Slack event: type=%s, envelope_id=%s", event_type, envelope.envelope_id)
        logger.debug("Full event payload: %s", event_data)

        try:
            if event_type == "app_mention":
                await self._handle_app_mention(event_data)
            elif event_type == "message":
                await self._handle_message(event_data)
            else:
                logger.debug("Ignoring event type: %s", event_type)
        except Exception:
            logger.exception("Error handling event type %s", event_type)

    async def _fetch_thread_context(
        self,
        channel: str,
        thread_ts: str,
        exclude_ts: str | None = None,
    ) -> str:
        """Fetch Slack thread history and format as context for the agent prompt.

        Args:
            channel: Channel ID
            thread_ts: Thread parent timestamp
            exclude_ts: Message timestamp to exclude (the current message)

        Returns:
            Formatted thread history string, or empty string if no history.
        """
        try:
            replies = await self._api.get_thread_replies(channel, thread_ts)
        except Exception:
            logger.warning("Failed to fetch thread history", exc_info=True)
            return ""

        if not replies:
            return ""

        # Exclude the current message to avoid duplicating it in the prompt
        if exclude_ts:
            replies = [r for r in replies if r.get("ts") != exclude_ts]

        if not replies:
            return ""

        # Resolve user display names (cached)
        user_ids = {msg["user"] for msg in replies if msg.get("user")}
        for uid in user_ids:
            if uid not in self._user_name_cache:
                try:
                    info = await self._api.get_user_info(uid)
                    self._user_name_cache[uid] = info.get("real_name") or info.get("name") or uid
                except Exception:
                    self._user_name_cache[uid] = uid

        # Format each message
        lines = []
        for msg in replies:
            user_id = msg.get("user", "")
            name = self._user_name_cache.get(user_id, user_id) if user_id else "bot"
            text = msg.get("text", "")
            lines.append(f"{name}: {text}")

        if not lines:
            return ""

        context = "\n".join(lines)

        # Cap total length to avoid overwhelming the prompt
        max_context_length = 20_000
        if len(context) > max_context_length:
            context = "...(earlier messages trimmed)...\n" + context[-max_context_length:]

        return (
            "Here is the conversation history from this Slack thread:\n\n" + context + "\n\n---\n\n"
        )

    async def _handle_app_mention(self, event: dict[str, Any]) -> None:
        """Handle app_mention event (new session).

        Args:
            event: App mention event data
        """
        # DEBUG: Log the mention event
        logger.info(
            "app_mention event received: user=%s, text=%s",
            event.get("user"),
            event.get("text", "")[:100],
        )

        try:
            mention_event = AppMentionEvent.model_validate(event)
        except Exception:
            logger.exception("Failed to parse app_mention event: %s", event)
            return

        # Create session ID from channel, thread, and agent name
        thread_ts = mention_event.thread_ts or mention_event.ts
        if self._agent_name:
            session_id = f"slack:{mention_event.channel}:{thread_ts}:{self._agent_name}"
        else:
            session_id = f"slack:{mention_event.channel}:{thread_ts}"

        # DEBUG: Log what sessions we have
        logger.info(
            "Checking for existing session %s. Current sessions: %s",
            session_id,
            list(self._sessions.keys()),
        )

        # Check if session already exists (avoid duplicates)
        if session_id in self._sessions:
            logger.info("Session %s already exists, ignoring duplicate mention", session_id)
            return

        # Extract prompt (strip bot mention)
        prompt = re.sub(r"<@\w+>\s*", "", mention_event.text).strip()
        if not prompt:
            # Empty prompt, just acknowledge
            await self._api.post_message(
                channel=mention_event.channel,
                text="Hi! Please include a message when you @mention me.",
                thread_ts=thread_ts,
            )
            return

        # Fetch thread history if this mention is inside an existing thread
        thread_context = ""
        if mention_event.thread_ts:
            thread_context = await self._fetch_thread_context(
                mention_event.channel, thread_ts, exclude_ts=mention_event.ts
            )

        logger.info(
            "New Slack session: %s (channel=%s, thread=%s, has_thread_context=%s)",
            session_id,
            mention_event.channel,
            thread_ts,
            bool(thread_context),
        )

        # Post initial "thinking" message
        try:
            response = await self._api.post_message(
                channel=mention_event.channel,
                text="🤔 Thinking...",
                thread_ts=thread_ts,
            )
            progress_ts = response["ts"]
        except Exception:
            logger.exception("Failed to post initial message")
            return

        # Track session
        session_data = {
            "channel": mention_event.channel,
            "thread_ts": thread_ts,
            "original_ts": mention_event.ts,
            "progress_message_ts": progress_ts,
            "current_text": "🤔 Thinking...",
        }
        self._sessions[session_id] = session_data

        # Mark this thread as active (will persist after session ends)
        self._active_threads.add((mention_event.channel, thread_ts))

        # Resolve which repo this channel should use (uses global installation ID for auth)
        channel_repos = settings.parsed_slack_channel_repos
        channel_repo = channel_repos.get(mention_event.channel, "")

        # Prepend channel-specific context to the prompt if configured
        channel_prompts = settings.parsed_slack_channel_prompts
        channel_context = channel_prompts.get(mention_event.channel, "")
        full_prompt = f"{channel_context}\n\n{prompt}" if channel_context else prompt

        # Create bridge request
        request = BridgeSessionRequest(
            external_session_id=session_id,
            service_name=self.service_name,
            prompt=thread_context + full_prompt,
            agent_name=self._agent_name,
            descriptive_name=slugify(prompt[:60]),
            service_metadata=session_data,
            github_repo=channel_repo,
        )

        # Start agent session
        await self._session_manager.handle_new_session(self, request)

    async def _handle_message(self, event: dict[str, Any]) -> None:
        """Handle message event (follow-up in thread).

        Args:
            event: Message event data
        """
        # DEBUG: Log message event details
        logger.info(
            "message event: subtype=%s, user=%s, bot_id=%s, text=%s",
            event.get("subtype"),
            event.get("user"),
            event.get("bot_id"),
            event.get("text", "")[:100],
        )

        try:
            message_event = MessageEvent.model_validate(event)
        except Exception:
            logger.exception("Failed to parse message event: %s", event)
            return

        # Ignore bot messages (including our own!)
        # Check for bot_id (our bot posts) or bot_profile (Slack's format)
        if (
            message_event.subtype == "bot_message"
            or not message_event.user
            or event.get("bot_id")
            or event.get("bot_profile")
        ):
            logger.debug("Ignoring bot message")
            return

        # Only handle messages in threads
        thread_ts = message_event.thread_ts
        if not thread_ts:
            return

        # Require @mention of this bot — don't respond to untagged thread messages
        text = message_event.text or ""
        if not self._bot_user_id or f"<@{self._bot_user_id}>" not in text:
            return

        # Check if this thread is one where the bot has been mentioned
        thread_key = (message_event.channel, thread_ts)
        if thread_key not in self._active_threads:
            logger.debug("Thread %s not active, ignoring message", thread_key)
            return

        if self._agent_name:
            session_id = f"slack:{message_event.channel}:{thread_ts}:{self._agent_name}"
        else:
            session_id = f"slack:{message_event.channel}:{thread_ts}"

        # Strip the @mention and get the prompt text
        prompt = re.sub(r"<@\w+>\s*", "", text).strip()
        if not prompt:
            return

        logger.info("Thread message for %s: %s", session_id, prompt[:100])

        # Always create a NEW progress message for follow-ups
        # (Even if we have session data, we want a fresh message, not to edit the old one)
        logger.info("Creating new progress message for follow-up in thread %s", session_id)
        try:
            response = await self._api.post_message(
                channel=message_event.channel,
                text="🤔 Thinking...",
                thread_ts=thread_ts,
            )
            progress_ts = response["ts"]
        except Exception:
            logger.exception("Failed to post progress message for follow-up")
            return

        # Update session tracking with new progress message
        if session_id in self._sessions:
            # Update existing session data with new progress message
            self._sessions[session_id]["progress_message_ts"] = progress_ts
            self._sessions[session_id]["current_text"] = "🤔 Thinking..."
        else:
            # Create new session tracking
            self._sessions[session_id] = {
                "channel": message_event.channel,
                "thread_ts": thread_ts,
                "original_ts": message_event.ts,
                "progress_message_ts": progress_ts,
                "current_text": "🤔 Thinking...",
            }

        # Fetch thread history so the agent sees messages from other users/agents
        thread_context = await self._fetch_thread_context(
            message_event.channel, thread_ts, exclude_ts=message_event.ts
        )

        # Send to session manager - it will resume the conversation with full history
        logger.info("Sending to session manager (will resume with history): %s", session_id)
        await self._session_manager.handle_followup(session_id, thread_context + prompt)

    async def send_update(self, session_id: str, update: BridgeUpdate) -> None:
        """Translate BridgeUpdate to Slack message edits (implements ServiceAdapter.send_update).

        Args:
            session_id: External session ID
            update: Bridge update to send
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            logger.warning("No session data for %s, cannot send update", session_id)
            return

        try:
            channel = session_data["channel"]
            ts = session_data["progress_message_ts"]

            if update.type == "thought":
                # Show current thought
                new_text = f"💭 {update.content}"
                new_text = _truncate_for_slack(new_text)
                await self._safe_update_message(channel, ts, new_text)
                session_data["current_text"] = new_text

            elif update.type == "tool_call":
                # Append tool execution to progress message
                current = session_data.get("current_text", "")
                tool_name = update.content
                new_text = f"{current}\n⚙️ `{tool_name}`"
                # Trim old lines from the top if too long
                if len(new_text) > SLACK_MAX_MESSAGE_LENGTH:
                    lines = new_text.split("\n")
                    while len("\n".join(lines)) > SLACK_MAX_MESSAGE_LENGTH and len(lines) > 1:
                        lines.pop(0)
                    new_text = "_(earlier tool calls trimmed)_\n" + "\n".join(lines)
                new_text = _truncate_for_slack(new_text)
                await self._safe_update_message(channel, ts, new_text)
                session_data["current_text"] = new_text

            elif update.type == "message_chunk":
                # Accumulate message text for final response
                self._message_buffers.setdefault(session_id, "")
                self._message_buffers[session_id] += update.content

            elif update.type == "plan":
                # Format plan as structured list
                entries = update.metadata.get("entries", [])
                plan_text = "📋 *Plan:*\n"
                status_icons = {
                    "pending": "⏳",
                    "in_progress": "▶️",
                    "completed": "✅",
                }
                for entry in entries:
                    icon = status_icons.get(entry.get("status", "pending"), "⏳")
                    content = entry.get("content", "")
                    plan_text += f"{icon} {content}\n"

                plan_text = _truncate_for_slack(plan_text)
                await self._safe_update_message(channel, ts, plan_text)
                session_data["current_text"] = plan_text

        except Exception:
            logger.exception("Error sending update to Slack for %s", session_id)

    async def send_completion(self, session_id: str, message: str, session_url: str = "") -> None:
        """Send completion message (implements ServiceAdapter.send_completion).

        Args:
            session_id: External session ID
            message: Completion message
            session_url: Optional link to the session viewer
        """
        # Keep session data for thread continuations (don't pop)
        session_data = self._sessions.get(session_id)
        if not session_data:
            logger.warning("No session data for %s, cannot send completion", session_id)
            return

        # Use accumulated message if available, otherwise use provided message
        final_text = self._message_buffers.pop(session_id, "") or message

        # Append session viewer link if available
        if session_url:
            final_text += f"\n\n<{session_url}|View full session>"

        # Slack has a ~40k character limit; truncate if needed
        final_text = _truncate_for_slack(final_text)

        try:
            channel = session_data["channel"]
            progress_ts = session_data["progress_message_ts"]
            original_ts = session_data["original_ts"]

            # Update progress message with final response
            await self._safe_update_message(channel, progress_ts, final_text)

            # Add checkmark reaction to original mention
            await self._api.add_reaction(channel, original_ts, "white_check_mark")

            logger.info("Completed session %s", session_id)

        except Exception:
            logger.exception("Error sending completion to Slack for %s", session_id)

    async def send_error(self, session_id: str, error: str) -> None:
        """Send error message (implements ServiceAdapter.send_error).

        Args:
            session_id: External session ID
            error: Error message
        """
        # Keep session data for potential retry (don't pop)
        session_data = self._sessions.get(session_id)
        self._message_buffers.pop(session_id, None)

        if not session_data:
            logger.warning("No session data for %s, cannot send error", session_id)
            return

        try:
            channel = session_data["channel"]
            progress_ts = session_data["progress_message_ts"]
            original_ts = session_data["original_ts"]

            # Update progress message with error
            error_text = f"❌ Error: {error}"
            await self._safe_update_message(channel, progress_ts, error_text)

            # Add X reaction to original mention
            await self._api.add_reaction(channel, original_ts, "x")

            logger.error("Error in session %s: %s", session_id, error)

        except Exception:
            logger.exception("Error sending error to Slack for %s", session_id)

    async def close(self) -> None:
        """Clean up resources (implements ServiceAdapter.close)."""
        await self._socket_client.stop()
        await self._api.close()
        logger.info("Slack adapter closed")
