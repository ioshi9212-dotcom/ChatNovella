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
from mcp.server.transport_security import TransportSecuritySettings
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
    engine_root=BASE,
)
PUBLIC_HOST = os.getenv(
    "CHATNOVELLA_PUBLIC_HOST", "chatnovella-yumikofv.waw0.amvera.tech"
).strip().lower()
mcp = FastMCP(
    "ChatNovella",
    instructions=(BASE / "docs/PLUGIN_INSTRUCTIONS.md").read_text(encoding="utf-8"),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            PUBLIC_HOST, f"{PUBLIC_HOST}:443",
            "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*",
            "[::1]", "[::1]:*",
        ],
        allowed_origins=[
            f"https://{PUBLIC_HOST}", f"https://{PUBLIC_HOST}:443",
            "https://chatgpt.com",
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
        ],
    ),
)


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
    """Fill state/NPC templates, or repair this same session BEFORE turn 1.

    Example: {"characters/NPC_001/core.md":"..."}.
    Missing facts require reader decision: supply, allow invention, or skip.
    A ready session with zero saved turns can be corrected without recreation.
    This reopens setup: call get_setup_report and finish_setup again, then fetch
    a new turn packet. Original and session ID stay unchanged. After turn 1,
    setup editing is forbidden.
    """
    return store.update_setup(session_id, expected_version, files)


@mcp.tool()
def get_setup_report(session_id: str) -> dict[str, Any]:
    """Check all eight NPC modules, including abilities.md, and setup gaps.

    Missing/empty required files block launch even with approved gaps. For
    unknown facts ask the reader: supply, invent with consent, or mark unknown.
    """
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
    """Read all eight engine/ instructions every turn, then session files.

    Static engine files always come first and are loaded fresh from the
    deployed Amvera app; they are never duplicated into session state.
    Entire content is delivered in lossless 36k-character chunks.

    Example: [{"id":"NPC_001","mode":"physical"}].
    Modes physical: all; phone: voice, personality, knowledge, relations;
    observer: personality, abilities, knowledge, relations;
    message/mentioned: no card. need_past=true optionally for phone/observer.
    Always fetch ALL chunks before writing narrative.
    For narrative generation: FIRST call this tool with participants=[] and
    read all chunks; run plan_novella_scene BEFORE drafting, then call this
    tool again with actual participants and read the final packet.
    Then apply engine/scene_review.md and call review_novella_scene.
    Follow the current review protocol and rewrite failed drafts before saving.
    """
    return store.prepare_turn(session_id, participants)


@mcp.tool()
def read_novella_chunk(packet_id: str, index: int) -> dict[str, Any]:
    """Fetch a complete context/audit chunk, zero-based index from 0 to count-1."""
    return store.read_chunk(packet_id, index)


@mcp.tool()
def plan_novella_scene(
    session_id: str, expected_version: int, reader_text: str,
    packet_id: str, plan: dict[str, Any],
) -> dict[str, Any]:
    """MANDATORY pre-writing planning gate: study ALL active arcs and NPCs first.

    FIRST call prepare_novella_turn with participants=[] and read every chunk,
    including the scout index of NPC cards. Then call this tool BEFORE writing
    narration. Only AFTER planning choose physical/phone/observer participants,
    request a new prepare_novella_turn packet, and read every chunk.

    plan keys:
      basis: scene situation and relevant existing context
      active_arcs: IDs ARC_*/Q_* actually found in state/arcs.md, [] if none
      candidate_npcs: IDs NPC_* considered, [] for a justified private pause
      time_span: elapsed time and what can happen meanwhile
      npc_initiative: motives, knowledge, opportunities and obstacles of NPCs
      intended_shift: planned change of at least one situation/arc
      possible_hook: how reader can naturally continue after the scene

    Planning does not force a character, conflict, or supernatural event.
    The reader controls POV. Server records preflight before review and will
    reject a scene without it; meaning remains the model's responsibility.
    """
    return store.plan_scene(session_id, expected_version, reader_text, packet_id, plan)


@mcp.tool()
def review_novella_scene(
    session_id: str, expected_version: int, reader_text: str,
    narration: str, files: dict[str, str], timeline_events: list[str],
    packet_id: str, checks: dict[str, dict[str, str]],
    relationship_labels: dict[str, str], timeline_no_change_reason: str = "",
) -> dict[str, Any]:
    """Review the complete UNPUBLISHED draft before save; does not add a turn.

    Read engine/scene_review.md. checks has exactly eight keys: canon, pov,
    npc, continuity, direction, knowledge, skin, persistence. Each value is
    {"status":"pass" or "fail", "evidence":"concrete result, 12–350 chars"}.
    npc also has relationships, a JSON STRING mapping each profiled participant
    (and each offscreen updated relation card) to {"status":"changed" or
    "unchanged","reason":"specific event/no-change reason, 12–240 chars"}.
    No numeric relationships for incidental actors without a core.md card.
    Check actual text against all loaded rules/cards; report flaws honestly.
    The author's checks are ADDITIONAL and never grant approval alone:
    the server calls a separate model to audit canon, relationships,
    NPC character and initiative, POV presence and voice, plot movement,
    pacing, genre-specific detail, originality, knowledge and world physics.
    Requires OPENAI_API_KEY on Amvera; when unavailable, review cannot pass.
    A pre-writing plan_novella_scene call is mandatory and must be registered
    before this review. In direction evidence, quote exact text from the
    narrative body for both the plot shift and the next-turn hook.

    relationship_labels maps each physical/phone/observer NPC ID to the exact
    visible name/description in its separate footer line. {} when none.
    Server checks header, 3x3 options, footer and exact saved relation numbers.
    Pass exact intended save arguments, including files and chronology.

    If approved=false, read independent_review and issues, rewrite the
    unpublished scene, then submit the NEW full draft for another review.
    No arbitrary two-rewrite limit. Identical requests are cached.
    Never save or publish a failed/unverified draft.
    approved=true returns review_id for save_novella_turn with identical inputs.
    """
    return store.review_scene(
        session_id, expected_version, reader_text, narration, files,
        timeline_events, packet_id, checks, relationship_labels,
        timeline_no_change_reason,
    )


@mcp.tool()
def save_novella_turn(
    session_id: str, expected_version: int, reader_text: str,
    narration: str, files: dict[str, str], timeline_events: list[str],
    packet_id: str, review_id: str, timeline_no_change_reason: str = "",
) -> dict[str, Any]:
    """Save ONLY the exact approved draft after review_novella_scene.

    review_id must come from approved=true. Narration, reader_text, files,
    chronology and packet_id must match that review exactly; changing any of
    them invalidates approval. Publish the saved text unchanged, no checklist.

    packet_id is returned by prepare_novella_turn. Read ALL chunks using
    read_novella_chunk before save; server verifies receipts and version.
    This confirms delivery, not whether GPT reasoned correctly about facts.

    The scene requires an explicit chronology decision.

    timeline_events: concise confirmed events ["02.05.2024 | произошло ..."].
    Send an empty list ONLY when no significant event occurred; then provide
    timeline_no_change_reason. Amvera appends dated events to timeline.md.
    Do NOT put state/timeline.md in files: it is written by the server.
    An omitted chronology decision rejects the entire turn before saving.
    """
    return store.commit_turn(
        session_id, expected_version, reader_text, narration, files,
        timeline_events=timeline_events,
        timeline_no_change_reason=timeline_no_change_reason,
        packet_id=packet_id,
        review_id=review_id,
    )


@mcp.tool()
def initialize_novella_relationship_metrics(
    session_id: str, expected_version: int,
    metrics: dict[str, dict[str, int]], basis: str,
) -> dict[str, Any]:
    """Add missing NPC-to-POV numeric scales without a new gameplay turn.

    Read the actual relation cards first. Supply 1–10 individual scales per
    existing NPC, with initial integer values 0..100; delta is recorded as 0.
    Example: {"NPC_001":{"интерес":65,"доверие":20}}.
    Explain the canon basis. These are initial game-mechanic estimates unless
    genuine earlier numeric values exist in revision history. In that case
    restore the historical values reported by the server instead of guessing.
    Existing numeric scales cannot be overwritten. Missing scales from an
    explicit author list in core.md can be completed with this same tool;
    historical values take priority, existing values/deltas remain untouched.
    Prose, original and game
    transcript are preserved; the state version changes, gameplay turn does not.
    """
    return store.initialize_relationship_metrics(session_id, expected_version, metrics, basis)


@mcp.tool()
def repair_novella_timeline(
    session_id: str, expected_version: int,
    source_turns: list[int], timeline_events: list[str],
) -> dict[str, Any]:
    """Backfill confirmed facts from EXISTING saved turns, no new game turn.

    Check exact saved scenes first. Provide format:
    ["02.05.2024 | факт из хода 1", "02.05.2024 | факт из хода 2"].
    The listed source_turns must exist in the saved journal. Only the
    chronology file is changed, with version check and revision backup.
    """
    return store.repair_timeline(
        session_id, expected_version, source_turns, timeline_events
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
