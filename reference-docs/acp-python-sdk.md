# ACP Python SDK Reference

Local source: `/tmp/acp-python-sdk/`

## Key File Locations

| Component | File Path |
|-----------|-----------|
| Client Protocol | `src/acp/interfaces.py` |
| ClientSideConnection | `src/acp/client/connection.py` |
| AgentSideConnection | `src/acp/agent/connection.py` |
| Core functions | `src/acp/core.py` |
| Schema types | `src/acp/schema.py` |
| Helpers | `src/acp/helpers.py` |
| Permissions contrib | `src/acp/contrib/permissions.py` |
| Session state contrib | `src/acp/contrib/session_state.py` |
| Tool calls contrib | `src/acp/contrib/tool_calls.py` |
| Example client | `examples/client.py` |
| Example agent | `examples/agent.py` |

## Installation

```
pip install agent-client-protocol
```

## Core Imports

```python
from acp import (
    PROTOCOL_VERSION,
    Client,
    Agent,
    RequestError,
    connect_to_agent,
    run_agent,
    text_block,
    image_block,
    audio_block,
    resource_block,
    tool_content,
    start_tool_call,
    update_agent_message,
    session_notification,
)
from acp.core import ClientSideConnection
from acp.schema import (
    ClientCapabilities,
    Implementation,
    NewSessionResponse,
    PromptResponse,
    AgentMessageChunk,
    AgentThoughtChunk,
    AgentPlanUpdate,
    ToolCallStart,
    ToolCallProgress,
    AvailableCommandsUpdate,
    CurrentModeUpdate,
    ConfigOptionUpdate,
    SessionInfoUpdate,
    UserMessageChunk,
    TextContentBlock,
    ImageContentBlock,
    AudioContentBlock,
    ResourceContentBlock,
    EmbeddedResourceContentBlock,
    PermissionOption,
    ToolCall,
    ToolCallUpdate,
    EnvVariable,
    CreateTerminalResponse,
    TerminalOutputResponse,
    ReleaseTerminalResponse,
    WaitForTerminalExitResponse,
    KillTerminalCommandResponse,
    ReadTextFileResponse,
    WriteTextFileResponse,
    RequestPermissionResponse,
    PlanEntry,
)
```

## Client Protocol (what you implement)

The `Client` Protocol is defined in `src/acp/interfaces.py`. You implement this to handle callbacks from the agent.

```python
class Client(Protocol):
    async def request_permission(
        self, options: list[PermissionOption], session_id: str, tool_call: ToolCallUpdate, **kwargs
    ) -> RequestPermissionResponse

    async def session_update(
        self, session_id: str,
        update: UserMessageChunk | AgentMessageChunk | AgentThoughtChunk
               | ToolCallStart | ToolCallProgress | AgentPlanUpdate
               | AvailableCommandsUpdate | CurrentModeUpdate
               | ConfigOptionUpdate | SessionInfoUpdate,
        **kwargs
    ) -> None

    async def write_text_file(self, content: str, path: str, session_id: str, **kwargs) -> WriteTextFileResponse | None
    async def read_text_file(self, path: str, session_id: str, limit=None, line=None, **kwargs) -> ReadTextFileResponse
    async def create_terminal(self, command: str, session_id: str, args=None, cwd=None, env=None, output_byte_limit=None, **kwargs) -> CreateTerminalResponse
    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs) -> TerminalOutputResponse
    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs) -> ReleaseTerminalResponse | None
    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs) -> WaitForTerminalExitResponse
    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs) -> KillTerminalCommandResponse | None
    async def ext_method(self, method: str, params: dict) -> dict
    async def ext_notification(self, method: str, params: dict) -> None
    def on_connect(self, conn: Agent) -> None
```

## Spawning an Agent & Connecting

```python
import asyncio
import asyncio.subprocess as aio_subprocess
from acp import PROTOCOL_VERSION, Client, connect_to_agent, text_block
from acp.schema import ClientCapabilities, Implementation

# 1. Spawn subprocess
proc = await asyncio.create_subprocess_exec(
    "claude-code-acp",
    stdin=aio_subprocess.PIPE,
    stdout=aio_subprocess.PIPE,
)

# 2. Connect (returns ClientSideConnection)
client_impl = MyClient()
conn = connect_to_agent(client_impl, proc.stdin, proc.stdout)

# 3. Initialize handshake
await conn.initialize(
    protocol_version=PROTOCOL_VERSION,
    client_capabilities=ClientCapabilities(),
    client_info=Implementation(name="my-client", title="My Client", version="0.1.0"),
)

# 4. Create session
session = await conn.new_session(mcp_servers=[], cwd="/path/to/project")

# 5. Send prompt (blocks until agent completes turn)
response = await conn.prompt(
    session_id=session.session_id,
    prompt=[text_block("Fix the bug in auth.py")],
)
# response.stop_reason: "end_turn" | "max_tokens" | "max_turn_requests" | "refusal" | "cancelled"

# 6. Cancel (notification, doesn't wait)
await conn.cancel(session_id=session.session_id)

# 7. Cleanup
proc.terminate()
await proc.wait()
```

## ClientSideConnection Methods

Defined in `src/acp/client/connection.py`:

| Method | Returns | Description |
|--------|---------|-------------|
| `initialize(protocol_version, client_capabilities, client_info)` | `InitializeResponse` | Protocol handshake |
| `new_session(cwd, mcp_servers)` | `NewSessionResponse` | Create new session (has `.session_id`) |
| `load_session(cwd, mcp_servers, session_id)` | `LoadSessionResponse` | Resume existing session |
| `list_sessions(cursor, cwd)` | `ListSessionsResponse` | List available sessions |
| `set_session_mode(mode_id, session_id)` | `SetSessionModeResponse` | Switch agent mode |
| `set_session_model(model_id, session_id)` | `SetSessionModelResponse` | Change model |
| `prompt(prompt, session_id)` | `PromptResponse` | Send prompt, wait for completion |
| `fork_session(cwd, session_id, mcp_servers)` | `ForkSessionResponse` | Fork a session |
| `resume_session(cwd, session_id, mcp_servers)` | `ResumeSessionResponse` | Resume session |
| `cancel(session_id)` | `None` | Cancel current turn (notification) |
| `close()` | `None` | Close connection |

## Session Update Types (received in `session_update` callback)

Discriminated by class type:

| Type | Key Fields | Description |
|------|-----------|-------------|
| `AgentMessageChunk` | `.content` (TextContentBlock, ImageContentBlock, etc.) | Agent response text streaming |
| `AgentThoughtChunk` | `.content` (TextContentBlock, etc.) | Agent internal reasoning |
| `ToolCallStart` | `.tool_call_id`, `.title`, `.kind`, `.status`, `.content`, `.locations` | New tool call initiated |
| `ToolCallProgress` | `.tool_call_id`, `.title`, `.kind`, `.status`, `.content` | Tool call status update |
| `AgentPlanUpdate` | `.entries` (list of PlanEntry) | Agent execution plan |
| `UserMessageChunk` | `.content` | User message echo |
| `AvailableCommandsUpdate` | `.available_commands` | Available commands changed |
| `CurrentModeUpdate` | `.current_mode_id` | Mode changed |
| `ConfigOptionUpdate` | config options | Config changed |
| `SessionInfoUpdate` | session info | Session info changed |

### ToolCallStart/ToolCallProgress Fields

- `tool_call_id: str` - unique ID
- `title: str` - human-readable description
- `kind: ToolKind | None` - "read", "edit", "delete", "move", "search", "execute", "think", "fetch", "other"
- `status: ToolCallStatus | None` - "pending", "in_progress", "completed", "failed"
- `content: list[ContentToolCallContent | FileEditToolCallContent | TerminalToolCallContent] | None`
- `locations: list[ToolCallLocation] | None` - affected file paths (`.path`)
- `raw_input: Any | None`
- `raw_output: Any | None`

### PlanEntry Fields

- `content: str` - task description
- `priority: PlanEntryPriority` - "low", "medium", "high"
- `status: PlanEntryStatus` - "pending", "in_progress", "completed"

## Permission Model

There is **no `bypassPermissions` setting** in the ACP protocol. Permissions are request-based:

```python
async def request_permission(self, options, session_id, tool_call, **kwargs):
    # Auto-approve everything:
    return RequestPermissionResponse(
        outcome=RequestPermissionOutcome(outcome="selected", option_id=options[0].option_id)
    )
```

Note: Check `src/acp/schema.py` for `RequestPermissionOutcome` — the exact field names. The `options` list contains `PermissionOption` with fields `option_id`, `name`, `kind` (allow_once, allow_always, reject_once, reject_always).

## Helper Functions

From `src/acp/helpers.py`:

```python
text_block(text) -> TextContentBlock
image_block(data, mime_type) -> ImageContentBlock
plan_entry(content, priority="medium", status="pending") -> PlanEntry
update_plan(entries) -> AgentPlanUpdate
start_tool_call(tool_call_id, title, kind=None, status=None, ...) -> ToolCallStart
update_tool_call(tool_call_id, title=None, status=None, ...) -> ToolCallProgress
session_notification(session_id, update) -> SessionNotification
```

## Contrib Utilities

### SessionAccumulator (session state tracking)
```python
from acp.contrib.session_state import SessionAccumulator

accumulator = SessionAccumulator(session_id)
snapshot = accumulator.apply(notification)
```

### ToolCallTracker
```python
from acp.contrib.tool_calls import ToolCallTracker
tracker = ToolCallTracker(id_factory=lambda: str(uuid.uuid4()))
```

## Data Flow Summary

```
1. Client spawns agent subprocess
2. connect_to_agent(client_impl, stdin, stdout) -> ClientSideConnection
3. conn.initialize(...)
4. conn.new_session(cwd, mcp_servers=[]) -> session_id
5. conn.prompt(session_id, [text_block(...)]) -> blocks until turn complete
   During prompt execution:
   - Agent sends session/update notifications -> client.session_update() called
   - Agent may request permission -> client.request_permission() called
   - Agent may read/write files -> client.read_text_file/write_text_file called
   - Agent may create terminals -> client.create_terminal etc. called
6. PromptResponse returned with stop_reason
7. Can send more prompts or cancel
```
