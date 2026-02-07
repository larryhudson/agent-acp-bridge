# ServiceAdapter Protocol Refactor — Summary

## What We Changed

We refactored the `ServiceAdapter` protocol to properly support both **webhook-based** (Linear) and **socket-based** (Slack) adapters without hacky workarounds.

## Before (Problems)

The original protocol was designed only for webhooks:

```python
class ServiceAdapter(Protocol):
    service_name: str

    def register_routes(self, app: FastAPI) -> None:
        """Register webhook routes."""
        ...

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        """Parse webhook event."""
        ...

    # ... send_update, send_completion, send_error ...
```

**Issues for Socket Mode adapters**:
1. ❌ `register_routes()` — no HTTP routes needed, would be empty
2. ❌ `on_session_created()` — not called (events come via WebSocket, not HTTP)
3. ❌ No lifecycle methods — nowhere to start/stop background WebSocket connection
4. ❌ Cleanup required `hasattr()` checks — `close()` wasn't in protocol

## After (Solution)

Extended protocol with lifecycle methods and clear documentation:

```python
class ServiceAdapter(Protocol):
    """Supports both webhook and socket-based adapters.

    - Webhook adapters: implement register_routes() and on_session_created()
    - Socket adapters: implement start() for background tasks

    Methods marked optional can be no-ops if not needed.
    """

    service_name: str

    def register_routes(self, app: FastAPI) -> None:
        """Register routes. Optional: no-op for socket adapters."""
        ...

    async def start(self) -> None:
        """Start background tasks. Optional: no-op for webhook adapters."""
        ...

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        """Parse webhook event. Optional: NotImplementedError for socket adapters."""
        ...

    async def send_update(self, session_id: str, update: BridgeUpdate) -> None:
        """Required: send updates to service."""
        ...

    async def send_completion(self, session_id: str, message: str) -> None:
        """Required: send completion message."""
        ...

    async def send_error(self, session_id: str, error: str) -> None:
        """Required: send error message."""
        ...

    async def close(self) -> None:
        """Clean up resources. Optional: no-op if no cleanup needed."""
        ...
```

## Implementation Pattern

### Webhook Adapter (Linear)

```python
class LinearAdapter:
    service_name = "linear"

    def register_routes(self, app: FastAPI) -> None:
        @app.post("/webhooks/linear")
        async def handle_webhook(request: Request):
            # Handle webhook
            ...

    async def start(self) -> None:
        """No-op: webhook adapters don't need background tasks."""
        pass

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        # Parse Linear webhook payload
        ...

    # ... send_update, send_completion, send_error ...

    async def close(self) -> None:
        await self._api.close()
```

### Socket Adapter (Slack)

```python
class SlackAdapter:
    service_name = "slack"

    def register_routes(self, app: FastAPI) -> None:
        """No-op: Socket Mode doesn't use HTTP routes."""
        pass

    async def start(self) -> None:
        """Start WebSocket connection."""
        self._socket_client.start()

    async def on_session_created(self, event: Any) -> BridgeSessionRequest:
        """Not used: events handled via WebSocket callbacks."""
        raise NotImplementedError

    # ... send_update, send_completion, send_error ...

    async def close(self) -> None:
        await self._socket_client.stop()
        await self._api.close()
```

## Main App Integration

Clean, uniform lifecycle for all adapters:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    for adapter in _adapters:
        adapter.register_routes(app)  # Registers routes OR no-op
        await adapter.start()         # Starts background tasks OR no-op
        logger.info("Started adapter: %s", adapter.service_name)

    yield

    # Shutdown
    for adapter in _adapters:
        await adapter.close()  # No hasattr() check needed!
```

## Benefits

✅ **No hacky workarounds**: No `hasattr()` checks, no `NotImplementedError` in core methods
✅ **Clear contracts**: Documentation explains which methods are optional vs required
✅ **Type safety**: All methods are in the protocol, so type checkers work properly
✅ **Extensible**: Easy to add new adapter types (GraphQL subscriptions, gRPC, etc.)
✅ **Consistent lifecycle**: All adapters follow the same startup/shutdown pattern

## Files Changed

1. ✅ `app/core/types.py` — Extended `ServiceAdapter` protocol
2. ✅ `app/services/linear/adapter.py` — Added `start()` method (no-op)
3. ✅ `app/main.py` — Call `start()` and `close()` for all adapters
4. ✅ `SLACK_SOCKET_MODE_PLAN.md` — Updated to reflect new architecture

## Next Steps

Now that the architecture is clean, we can proceed with implementing the Slack adapter following the plan:

1. Add dependencies (websockets, slack-sdk)
2. Implement Slack models, API client, socket client
3. Implement SlackAdapter using the new protocol
4. Test and deploy

The refactored protocol makes the Slack integration straightforward and maintainable.
