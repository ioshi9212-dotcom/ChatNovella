"""Streamable HTTP MCP endpoint for ChatNovella on Amvera.

CHATNOVELLA_API_TOKEN is optional. When omitted, the endpoint has no
application-level authentication. Set a strong token to enable Bearer auth.
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from chatnovella.store import SessionStore

TOKEN = os.getenv("CHATNOVELLA_API_TOKEN", "")
if TOKEN and len(TOKEN) < 24:
    raise RuntimeError(
        "Set a strong CHATNOVELLA_API_TOKEN (>=24 characters) in Amvera secrets"
    )

BASE = Path(__file__).resolve().parents[1]
store = SessionStore(
    os.environ.get("CHATNOVELLA_DATA_DIR", "/data/chatnovella"),
    BASE / "templates/session",
)
mcp = FastMCP("ChatNovella", stateless_http=True, json_response=True)


@mcp.tool()
def list_novella_sessions() -> list[dict[str, Any]]:
    """List existing independent ChatNovella sessions without full private content."""
    return store.list_sessions()


@mcp.tool()
def create_novella_session(
    original_text: str, novel_text: str, pov_text: str,
) -> dict[str, Any]:
    """Only after reader confirms: preserve ORIGINAL verbatim and create setup state.

    Original cannot later be overwritten. Fill/setup the state before playing.
    """
    return store.create_session(original_text, novel_text, pov_text)


@mcp.tool()
def get_session_status(session_id: str) -> dict[str, Any]:
    """Resume by exact session ID; return status, version, turn and audits."""
    return store.read_state(session_id)


@mcp.tool()
def read_original_during_setup(session_id: str) -> str:
    """Read immutable original for setup/correction, never on normal turns."""
    return store.read_original(session_id)


@mcp.tool()
def save_setup_files(
    session_id: str, expected_version: int, files: dict[str, str],
) -> dict[str, Any]:
    """Fill state/NPC templates during setup with approved source facts.

    Example: {"characters/NPC_001/core.md":"..."}.
    Missing facts require reader decision: supply, allow invention, or skip.
    """
    return store.update_setup(session_id, expected_version, files)


@mcp.tool()
def get_setup_report(session_id: str) -> dict[str, Any]:
    """List gaps in setup. Ask the reader: fill herself, invent with consent, or skip."""
    return store.setup_report(session_id)


@mcp.tool()
def finish_setup(session_id: str, expected_version: int,
                 reader_approved_gaps: bool = False) -> dict[str, Any]:
    """After checks and gap decisions mark ready; wait for launch command."""
    return store.activate(session_id, expected_version, reader_approved_gaps)


@mcp.tool()
def prepare_novella_turn(
    session_id: str, participants: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prepare all common files and selected NPC sections in lossless 36k chunks.

    Example: [{"id":"NPC_001","mode":"physical"}].
    Modes physical: all; phone: voice, personality, knowledge, relations;
    observer: personality, abilities, knowledge, relations;
    message/mentioned: no card. need_past=true optionally for phone/observer.
    Always fetch ALL chunks before writing narrative.
    """
    return store.prepare_turn(session_id, participants)


@mcp.tool()
def read_novella_chunk(packet_id: str, index: int) -> dict[str, Any]:
    """Fetch a complete context/audit chunk, zero-based index from 0 to count-1."""
    return store.read_chunk(packet_id, index)


@mcp.tool()
def save_novella_turn(
    session_id: str, expected_version: int, reader_text: str,
    narration: str, files: dict[str, str],
) -> dict[str, Any]:
    """Commit a full turn BEFORE publishing. Must include state/scene.md.

    Include only other changed files. recent.md and exact transcript are updated
    by Amvera. Old version cannot overwrite the latest state from another chat.
    """
    return store.commit_turn(
        session_id, expected_version, reader_text, narration, files
    )


@mcp.tool()
def prepare_novella_audit(session_id: str, kind: int) -> dict[str, Any]:
    """Get 15-turn original CHAT vs files or 60-turn FILES vs FILES audit."""
    return store.prepare_audit(session_id, kind)


@mcp.tool()
def finish_novella_audit(
    session_id: str, expected_version: int, kind: int,
    files: dict[str, str],
) -> dict[str, Any]:
    """Commit audit changes and unblock playing. Empty files if no changes.

    15: check original turns and missed events; 60: check knowledge vs chronology,
    compress without dropping established truths.
    """
    return store.commit_audit(session_id, expected_version, kind, files)


class BearerMiddleware:
    """ASGI wrapper; preserves MCP lifespan (no BaseHTTPMiddleware pitfalls)."""

    def __init__(self, inner: Any, token: str):
        self.inner = inner
        self.token = token.encode("utf-8")

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any,
    ) -> None:
        if scope["type"] == "http":
            if scope.get("path") == "/health" and scope.get("method") == "GET":
                await JSONResponse({"status": "ok"})(scope, receive, send)
                return
            if self.token:
                headers = dict(scope.get("headers", []))
                candidate = headers.get(b"authorization", b"")
                expected = b"Bearer " + self.token
                if not hmac.compare_digest(candidate, expected):
                    await JSONResponse(
                        {"error": "unauthorized"}, status_code=401
                    )(scope, receive, send)
                    return
        await self.inner(scope, receive, send)


app = BearerMiddleware(mcp.streamable_http_app(), TOKEN)
