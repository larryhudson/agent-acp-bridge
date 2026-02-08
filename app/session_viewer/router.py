"""FastAPI routes for the agent session viewer."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from app.session_viewer.reader import read_session
from app.session_viewer.viewer_html import VIEWER_HTML

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["session-viewer"])


@router.get("/{session_id}", response_class=HTMLResponse)
async def view_session(session_id: str) -> HTMLResponse:
    """Serve the session viewer HTML page.

    The page loads session data from the /data endpoint via fetch().
    """
    # Inject the session ID into the HTML template
    html = VIEWER_HTML.replace(
        "window.SESSION_ID",
        f'window.SESSION_ID = "{session_id}"',
    )
    return HTMLResponse(content=html)


@router.get("/{session_id}/data")
async def session_data(session_id: str) -> JSONResponse:
    """Return parsed JSONL session data as a JSON array."""
    entries = read_session(session_id)
    if not entries:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(content=entries)
