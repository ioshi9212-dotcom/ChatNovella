"""Streamable HTTP MCP adapter for V2, using the same isolated REST engine.

The V2 server exposes /mcp without an API token. Keep the service private
at the hosting/network level if novels must not be public. Blocking store I/O stays in
Starlette's thread pool so a session lock cannot block the async server.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool

from .engine import Engine, EngineError
from .hooks import HookProblem
from .threads import ThreadProblem
from .mcp_models import (
    AnchorBlock, AuditReport, Checksum, Correction, HistoricalArc, HistoricalHook, StoryPlan, NewCharacterProposal,
    RangeBlock, Relationships,
    RequestId, ReviewCheck, RuleCheck, SessionId, State, Turn, Version,
)


def build_mcp(engine: Callable[[], Engine]) -> FastMCP:
    # Current Amvera application keeps its existing domain after the
    # main-branch V2 switchover; allow that host without a new secret.
    # Override only if the project uses a different actual hostname.
    host = os.getenv(
        "CHATNOVELLA_V2_PUBLIC_HOST",
        "chatnovella-yumikofv.waw0.amvera.tech",
    ).strip().lower()
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
        except HookProblem as exc:
            raise ToolError(f"INVALID_HOOK: {exc}") from None
        except ThreadProblem as exc:
            raise ToolError(f"INVALID_THREAD: {exc}") from None
        except (ValueError, TypeError, KeyError):
            raise ToolError("INVALID_VALUE: Invalid V2 field value or type") from None

    @server.tool(annotations=read)
    async def load_v2_instructions(rules_only: bool = False) -> dict[str, Any]:
        """Use rules_only=true at chat bootstrap before the director. Full pair only for explicit inspection; turn packets always supply fresh Rules and Skin Builder."""
        return await call("load_instructions", rules_only)

    @server.tool(annotations=read)
    async def get_v2_instruction_versions() -> dict[str, Any]:
        """Return only instruction hashes, without full texts or a gameplay turn."""
        return await call("instruction_versions")

    @server.tool(annotations=write)
    async def create_v2_novel(
        original: str, blocks: list[AnchorBlock] | list[RangeBlock],
        names: dict[str, str], genre_answers: dict[str, str] | None = None,
        relationships: Relationships | None = None,
        initial_threads: Any = None,
        initial_hooks: Any = None,
    ) -> dict[str, Any]:
        """Save NEW NV2_ canon verbatim. Omit legacy initial_threads/initial_hooks:
        initialize story memory with backfill_v2_hooks after canon approval.
        Preview full canon before approval. Never migrate LITE_.
        """
        return await call(
            "create", original, [b.model_dump() for b in blocks], names, genre_answers,
            relationships.model_dump() if relationships is not None else None,
            initial_threads, initial_hooks,
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

    @server.tool(annotations=read)
    async def add_v2_character(
        session_id: SessionId, expected_version: Version,
        name: str, display: str, established_text: str,
    ) -> dict[str, Any]:
        """Validate a temporary NPC proposal only. Pass returned proposal in submit_v2_scene_context.new_characters (or prepare_v2_context.new_characters in legacy mode); final successful scene save creates permanent files."""
        return await call("add_character", session_id, expected_version, name, display, established_text)

    @server.tool(annotations=repeat)
    async def get_v2_director_brief(session_id: SessionId) -> dict[str, Any]:
        """MANDATORY BEFORE selecting participants for a new scene: read ALL
        open arcs, independent unsolved hooks, explicit author appearance windows,
        NPC goals and full cast IDs. This is director-only, not NPC knowledge.
        Do not automatically force encounters, reveal hidden lore or invent events.
        """
        return await call("director_brief", session_id)

    @server.tool(annotations=write)
    async def backfill_v2_hooks(
        session_id: SessionId, expected_version: Version,
        additions: list[HistoricalHook], arc_repairs: list[HistoricalArc] | None = None,
    ) -> dict[str, Any]:
        """One-off safe existing-session story-memory backfill. Each hook MUST
        quote the original author text (origin_turn=0) or a real archived scene.
        Does not rewrite scenes, canon or relationships. Explicit arc_repairs can restore
        missing open arcs with archived evidence; use stable existing arc IDs.
        Supply origin_turn and optional last_turn/last_evidence separately.
        Reopen an incorrectly closed hook with reopen=true and the same identity.
        Reconcile an existing open hook with historical closure using reconcile=true.
        The latest turn cannot be used while rollback is possible.
        """
        return await call("backfill_hooks", session_id, expected_version,
                          [h.model_dump() for h in additions],
                          [a.model_dump() for a in (arc_repairs or [])])

    @server.tool(annotations=repeat)
    async def prepare_v2_context(
        session_id: SessionId, reader_text: str, participants: list[str],
        kind: Literal["turn", "audit", "canon"], request_id: RequestId,
        story_plan: StoryPlan | None = None,
        new_characters: list[NewCharacterProposal] | None = None,
    ) -> dict[str, Any]:
        """Call director brief first. With scene_context_enabled=true, use participants=[]
        and defer story_plan and new_characters until collection is complete. Read all
        collector chunks, then submit_v2_scene_context; no prose yet. On retries keep
        exact request_id/input. A reused director packet already contains the completed
        collection: read only its remaining chunks. Legacy mode keeps its original plan
        and participant selection. No proposals or plan for canon/audit."""
        return await call("prepare", session_id, reader_text, participants, kind, request_id,
                          story_plan.model_dump() if story_plan is not None else None,
                          [row.model_dump() for row in new_characters] if new_characters is not None else None)

    @server.tool(annotations=repeat)
    async def read_v2_chunk(session_id: SessionId, packet_id: str, index: int) -> dict[str, Any]:
        """Read next unread chunk in order; verify hashes. Collector phase contains raw
        facts and scene_builder only: after all_chunks_read submit the compiled context.
        Director phase returns the compilation and full Skin Builder; write only after
        all director chunks have been read. Audit/canon are unchanged."""
        return await call("read_chunk", session_id, packet_id, index)

    @server.tool(annotations=repeat)
    async def submit_v2_scene_context(
        session_id: SessionId, expected_version: Version, packet_id: str,
        request_id: RequestId, chunk_hashes: list[Checksum],
        scene_context: Annotated[str, Field(min_length=1, max_length=36000)],
        participants: list[str], story_plan: StoryPlan | None = None,
        new_characters: list[NewCharacterProposal] | None = None,
    ) -> dict[str, Any]:
        """MANDATORY in collector mode after all raw chunks. Send the actual compiled
        scene_context.md (not narration), dossiers for POV and every selected NPC,
        plot plan and tentative new characters. Selected NPCs are possible participants,
        not proof of appearance. Returns NEW director packet ID and hashes. Read all
        new chunks; only then write prose. Exact retries reuse the director packet."""
        return await call("submit_scene_context", session_id, expected_version,
                          packet_id, request_id, chunk_hashes, scene_context, participants,
                          story_plan.model_dump() if story_plan is not None else None,
                          [row.model_dump() for row in new_characters] if new_characters is not None else None)

    @server.tool(annotations=read)
    async def get_v2_review_manifest() -> dict[str, Any]:
        """Inspect retained strict reviewer only. Do not call for normal turns, especially when scene_review_enabled=false."""
        return await call("scene_review_manifest")

    @server.tool(annotations=repeat)
    async def review_v2_scene(
        session_id: SessionId, expected_version: Version,
        packet_id: str, request_id: RequestId, skin_sha256: Checksum,
        narration: str, checks: list[ReviewCheck],
        rule_checks: list[RuleCheck] | None = None,
    ) -> dict[str, Any]:
        """Only when prepare.scene_review_enabled=true. Disabled by default: skip this tool.
        In strict mode, audit EVERY section
        of the current full Skin Builder, including all individual rules.
        Fix any failed sections and rerun review on the exact final narration.
        Server checks mandatory formatting and binds approval to the final text.
        Never show an unapproved draft to the reader.
        """
        return await call("review_scene", session_id, expected_version,
                          packet_id, request_id, skin_sha256, narration,
                          [item.model_dump() for item in checks],
                          [item.model_dump() for item in (rule_checks or [])])

    @server.tool(annotations=repeat)
    async def save_v2_turn(session_id: SessionId, turn: Turn) -> dict[str, Any]:
        """In collector mode require the final director packet ID and hashes, not the raw collector packet.
        When scene_review_enabled=false, save final narration directly without review.
        Atomically save scene plus independent evidence-backed hook_updates. Never
        erase old unresolved hooks when advancing another one. Publish only after saved=true."""
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
