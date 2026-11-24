"""Streamable HTTP MCP adapter for V2, using the same isolated REST engine.

The V2 server exposes /mcp without an API token. Keep the service private
at the hosting/network level if novels must not be public. Blocking store I/O stays in
Starlette's thread pool so a session lock cannot block the async server.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool

from .engine import Engine, EngineError
from .mcp_models import (
    AnchorBlock, AuditReport, Checksum, Correction, RangeBlock, Relationships,
    RequestId, SessionId, State, ThreadUpdate, Turn, Version,
)


def build_mcp(engine: Callable[[], Engine]) -> FastMCP:
    host = os.getenv("CHATNOVELLA_V2_PUBLIC_HOST", "").strip().lower()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "testserver"]
    origins = ["https://chatgpt.com", "http://localhost:*", "http://127.0.0.1:*"]
    if host:
        if any(c in host for c in "/:@?# "):
            raise RuntimeError("CHATNOVELLA_V2_PUBLIC_HOST must contain only the actual public hostname")
        hosts += [host, host + ":*"]
        origins += ["https://" + host]
    server = FastMCP(
        "ChatNovella V2",
        instructions=Path(__file__).with_name("MCP_INSTRUCTIONS.md").read_text(encoding="utf-8"),
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts, allowed_origins=origins),
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    repeat = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                             idempotentHint=True, openWorldHint=False)

    async def call(method: str, *args, **kwargs) -> Any:
        try:
            return await run_in_threadpool(getattr(engine(), method), *args, **kwargs)
        except EngineError as exc:
            raise ToolError(f"{exc.code}: {exc}") from None
        except (ValueError, TypeError, KeyError):
            raise ToolError("INVALID_VALUE: Invalid V2 field value or type") from None

    @server.tool(annotations=read)
    async def load_v2_instructions() -> dict[str, Any]:
        """Read FULL Rules + author Skin Builder once per new chat or explicit refresh. Verify SHA-256. Never call every turn."""
        return await call("load_instructions")

    @server.tool(annotations=read)
    async def get_v2_instruction_versions() -> dict[str, Any]:
        """Return only instruction hashes, without full texts or a gameplay turn."""
        return await call("instruction_versions")

    @server.tool(annotations=write)
    async def create_v2_novel(
        original: str, blocks: list[AnchorBlock] | list[RangeBlock],
        names: dict[str, str], genre_answers: dict[str, str] | None = None,
        relationships: Relationships | None = None,
        initial_threads: list[ThreadUpdate] | None = None,
    ) -> dict[str, Any]:
        """Create NEW NV2_ setup, preserving original verbatim and assigning every character once. Preview full canon before approval. Never migrate LITE_."""
        return await call(
            "create", original, [b.model_dump() for b in blocks], names, genre_answers,
            relationships.model_dump() if relationships is not None else None,
            [t.model_dump() for t in initial_threads] if initial_threads is not None else None,
        )

    @server.tool(annotations=read)
    async def get_v2_status(session_id: SessionId) -> dict[str, Any]:
        """Read current V2 version, canon hash, approval, turn and required audit."""
        return await call("status", session_id)

    @server.tool(annotations=write)
    async def approve_v2_canon(
        session_id: SessionId, expected_version: Version, canon_hash: Checksum,
        confirmation: str,
    ) -> dict[str, Any]:
        """Only after explicit reader approval of the complete preview and exact canon_hash; does not create a scene."""
        return await call("approve", session_id, expected_version, canon_hash, confirmation)

    @server.tool(annotations=write)
    async def set_v2_opening(
        session_id: SessionId, expected_version: Version, state: State,
        relationships: Relationships | None = None,
    ) -> dict[str, Any]:
        """Set the approved starting physical scene at turn zero, with all eight state fields. Preserve author relationships and rule."""
        return await call("set_opening", session_id, expected_version, state.model_dump(),
                          relationships.model_dump() if relationships is not None else None)

    @server.tool(annotations=write)
    async def add_v2_character(
        session_id: SessionId, expected_version: Version,
        name: str, display: str, established_text: str,
    ) -> dict[str, Any]:
        """Register a genuinely appeared NPC with a stable English name and established text. Reprepare before using its card/knowledge."""
        return await call("add_character", session_id, expected_version, name, display, established_text)

    @server.tool(annotations=repeat)
    async def prepare_v2_context(
        session_id: SessionId, reader_text: str, participants: list[str],
        kind: Literal["turn", "audit", "canon"], request_id: RequestId,
    ) -> dict[str, Any]:
        """Keep one request_id per turn. Replay saved duplicates; resume only unread chunks. Every packet includes full relationships and open_threads, not Rules/Skin."""
        return await call("prepare", session_id, reader_text, participants, kind, request_id)

    @server.tool(annotations=repeat)
    async def read_v2_chunk(session_id: SessionId, packet_id: str, index: int) -> dict[str, Any]:
        """Read one UNREAD zero-based chunk and receipt. Verify its SHA-256 and joined packet hash; require all_chunks_read before commit."""
        return await call("read_chunk", session_id, packet_id, index)

    @server.tool(annotations=repeat)
    async def save_v2_turn(session_id: SessionId, turn: Turn) -> dict[str, Any]:
        """Atomically commit the SAME request_id/packet/hashes/input. Save only evidenced personal knowledge, real relationship/arc changes. Publish only after saved=true."""
        return await call("commit", session_id, turn.model_dump(exclude_unset=True))

    @server.tool(annotations=write)
    async def finish_v2_audit(
        session_id: SessionId, expected_version: Version, packet_id: str,
        chunk_hashes: list[Checksum], report: AuditReport,
        corrections: dict[str, Correction] | None = None,
    ) -> dict[str, Any]:
        """Finish required 15/60 audit after all verified chunks. Corrections require file hashes/reasons and witnessed evidence; never rewrite original/history or relationship rules."""
        return await call(
            "audit", session_id, expected_version, packet_id, chunk_hashes,
            report.model_dump(exclude_none=True),
            {k: v.model_dump(exclude_none=True) for k, v in (corrections or {}).items()},
        )

    @server.tool(annotations=read)
    async def search_v2_history(
        session_id: SessionId, start: int = 1, end: int | None = None, query: str = "",
    ) -> dict[str, Any]:
        """Read/search complete active saved V2 scenes by turn or exact text."""
        return {"scenes": await call("history", session_id, start, end, query)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def rollback_v2_turn(
        session_id: SessionId, expected_version: Version, request_id: RequestId, reason: str,
    ) -> dict[str, Any]:
        """Only on explicit reader request, roll back the exact last saved request_id using current version. Restore documents; retain cancelled scene in archive."""
        return await call("rollback", session_id, expected_version, request_id, reason)

    return server
