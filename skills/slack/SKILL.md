# Slack Web API

You can interact with Slack using the `SLACK_BOT_TOKEN` environment variable and `curl`.

## Searching Messages

Search requires the user token (`SLACK_USER_TOKEN`), not the bot token:

```bash
curl -s -H "Authorization: Bearer $SLACK_USER_TOKEN" \
  "https://slack.com/api/search.messages?query=YOUR_QUERY&count=10" | jq .
```

## Posting a Message

```bash
curl -s -X POST -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel": "CHANNEL_ID", "text": "Hello from the agent"}' \
  "https://slack.com/api/chat.postMessage" | jq .
```

## Listing Channels

```bash
curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  "https://slack.com/api/conversations.list?types=public_channel&limit=100" | jq '.channels[] | {id, name}'
```

## Reading Channel History

```bash
curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  "https://slack.com/api/conversations.history?channel=CHANNEL_ID&limit=20" | jq '.messages[] | {user, text, ts}'
```

## Reading a Thread

```bash
curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  "https://slack.com/api/conversations.replies?channel=CHANNEL_ID&ts=THREAD_TS" | jq '.messages[] | {user, text, ts}'
```

## Tips

- Always use `jq` to parse JSON responses for readability.
- The bot token has the scopes configured on the Slack App — typically `chat:write`, `channels:read`, `search:read`, etc.
- Channel IDs look like `C01ABC23DEF`. Use `conversations.list` to find them.
- For search, use Slack's search modifiers: `in:#channel`, `from:@user`, `before:2024-01-01`.
