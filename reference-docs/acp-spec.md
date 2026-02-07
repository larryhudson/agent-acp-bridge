# ACP Protocol Specification Reference

Local source: `/tmp/acp-repo/`

## Key Documents

| Document | Path |
|----------|------|
| Schema Definition | `schema/schema.json` (3430 lines, complete JSON Schema) |
| Protocol Overview | `docs/protocol/overview.mdx` |
| Session Setup | `docs/protocol/session-setup.mdx` |
| Prompt Turn | `docs/protocol/prompt-turn.mdx` |
| Tool Calls | `docs/protocol/tool-calls.mdx` |
| Initialization | `docs/protocol/initialization.mdx` |
| Transports | `docs/protocol/transports.mdx` |

## Protocol Basics

- **JSON-RPC 2.0** over stdio (stdin/stdout)
- Messages are **newline-delimited** (`\n`)
- UTF-8 encoded, no embedded newlines in messages
- Agent stdout: ONLY valid ACP messages
- Agent stderr: optional logging (client may ignore)
- Two message types: **methods** (request-response) and **notifications** (one-way)

## Lifecycle

### Phase 1: Initialization
```
Client -> Agent: initialize (protocol_version + capabilities)
Agent -> Client: initialize response (agreed version + agent capabilities)
```

### Phase 2: Session Setup
```
Client -> Agent: session/new (cwd + mcpServers)
Agent -> Client: session/new response (sessionId)
```

Or to resume:
```
Client -> Agent: session/load (sessionId + cwd + mcpServers)
Agent -> Client: session/update notifications (replay history)
Agent -> Client: session/load response
```

### Phase 3: Prompt Turn (repeating)
```
Client -> Agent: session/prompt (prompt blocks)
Agent -> Client: session/update notifications (streamed, many of these)
[Agent may also call: fs/read_text_file, fs/write_text_file, terminal/*, session/request_permission]
Agent -> Client: session/prompt response (stopReason)
```

### Phase 4: Cancellation
```
Client -> Agent: session/cancel (notification)
Agent -> Client: session/prompt response (stopReason: "cancelled")
```

## Client -> Agent Methods

| Method | Description |
|--------|-------------|
| `initialize` | Negotiate protocol version & capabilities |
| `authenticate` | Auth if required |
| `session/new` | Create new session |
| `session/load` | Resume previous session |
| `session/prompt` | Send user message |
| `session/set_mode` | Switch agent mode |
| `session/set_config_option` | Set configuration |
| `session/cancel` | Cancel ongoing turn (notification) |

## Agent -> Client Methods (Requests)

| Method | Description |
|--------|-------------|
| `fs/read_text_file` | Read file contents |
| `fs/write_text_file` | Write file contents |
| `terminal/create` | Create terminal |
| `terminal/output` | Get terminal output |
| `terminal/release` | Release terminal |
| `terminal/wait_for_exit` | Wait for command exit |
| `terminal/kill` | Kill terminal command |
| `session/request_permission` | Request user authorization |

## Agent -> Client Notifications

| Method | Description |
|--------|-------------|
| `session/update` | Stream updates during prompt turn |

## Session Update Types (in session/update)

| Type | Description |
|------|-------------|
| `user_message_chunk` | User message streaming |
| `agent_message_chunk` | Agent response streaming |
| `agent_thought_chunk` | Agent internal reasoning |
| `tool_call` | New tool call initiated |
| `tool_call_update` | Status/results update for tool call |
| `plan` | Agent execution plan |
| `available_commands_update` | Commands changed |
| `current_mode_update` | Mode changed |
| `config_option_update` | Config updated |

## Tool Call Fields

- `toolCallId`: unique within session
- `title`: human-readable description
- `kind`: read, edit, delete, move, search, execute, think, fetch, other
- `status`: pending -> in_progress -> completed/failed
- `content`: text, diffs, terminal references
- `locations`: affected file paths

## Stop Reasons

| Reason | Meaning |
|--------|---------|
| `end_turn` | LLM finished normally |
| `max_tokens` | Token limit reached |
| `max_turn_requests` | Max model requests exceeded |
| `refusal` | Agent refused |
| `cancelled` | Client cancelled |

## Permission Model

Agent requests via `session/request_permission` (RPC call, blocks until answered):

```json
{
  "sessionId": "sess_abc",
  "toolCall": { "toolCallId": "...", "title": "...", "kind": "execute" },
  "options": [
    { "optionId": "allow-once", "name": "Allow once", "kind": "allow_once" },
    { "optionId": "reject-once", "name": "Reject", "kind": "reject_once" }
  ]
}
```

Option kinds: `allow_once`, `allow_always`, `reject_once`, `reject_always`

Response:
```json
{ "outcome": { "outcome": "selected", "optionId": "allow-once" } }
```

**No `bypassPermissions` setting exists in the protocol.** To auto-approve, always respond with the first "allow" option.

## Capabilities Negotiation

Client advertises:
```json
{
  "fs": { "readTextFile": true, "writeTextFile": true },
  "terminal": true
}
```

Agent advertises:
```json
{
  "loadSession": true,
  "promptCapabilities": { "image": true, "audio": true, "embeddedContext": true },
  "mcpCapabilities": { "http": true, "sse": true }
}
```
