"""Isolated API for ChatNovella V2. Does not import or expose Lite sessions."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .engine import Engine, EngineError
from .mcp_adapter import build_mcp

store = Engine(os.getenv("CHATNOVELLA_V2_DATA_DIR", "./.novella_v2_data"))
mcp = build_mcp(lambda: store)
mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="ChatNovella V2", version="0.1.0", lifespan=lifespan)


@app.exception_handler(EngineError)
async def engine_error(_request: Request, exc: EngineError):
    return JSONResponse({"code": exc.code, "detail": str(exc)}, status_code=exc.status)


@app.exception_handler(ValueError)
async def invalid_value(_request: Request, exc: ValueError):
    return JSONResponse({"code": "INVALID_VALUE", "detail": str(exc)}, status_code=422)


@app.exception_handler(TypeError)
async def invalid_type(_request: Request, _exc: TypeError):
    return JSONResponse({"code": "INVALID_TYPE", "detail": "Invalid field type"}, status_code=422)


@app.exception_handler(KeyError)
async def missing_key(_request: Request, _exc: KeyError):
    return JSONResponse({"code": "MISSING_FIELD", "detail": "Required field missing"}, status_code=422)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "engine": "chatnovella-v2"}


@app.get("/v2/instructions")
def load_instructions():
    """Full Rules + Skin Builder, explicitly once per chat or when asked."""
    return store.load_instructions()


@app.get("/v2/instructions/versions")
def instruction_versions():
    """Small optional version check, without returning instruction texts."""
    return store.instruction_versions()


@app.post("/v2/novels")
def create(body: dict):
    return store.create(
        original=body["original"], blocks=body["blocks"],
        names=body["names"], genre_answers=body.get("genre_answers"),
        relationships=body.get("relationships"),
        initial_threads=body.get("initial_threads"),
        initial_hooks=body.get("initial_hooks"))


@app.get("/v2/novels/{sid}")
def status(sid: str):
    return store.status(sid)


@app.post("/v2/novels/{sid}/approve")
def approve(sid: str, body: dict):
    return store.approve(sid, body["expected_version"], body["canon_hash"],
                         body["confirmation"])


@app.post("/v2/novels/{sid}/opening")
def opening(sid: str, body: dict):
    return store.set_opening(sid, body["expected_version"], body["state"],
                             body.get("relationships"))


@app.post("/v2/novels/{sid}/characters")
def add_character(sid: str, body: dict):
    return store.add_character(sid, body["expected_version"], body["name"],
                               body["display"], body["established_text"])


@app.get("/v2/novels/{sid}/director")
def director(sid: str):
    """Read director-only open obligations BEFORE choosing participants."""
    return store.director_brief(sid)


@app.post("/v2/novels/{sid}/hooks/backfill")
def hooks_backfill(sid: str, body: dict):
    return store.backfill_hooks(
        sid, body["expected_version"], body["additions"], body.get("arc_repairs"))


@app.post("/v2/novels/{sid}/prepare")
def prepare(sid: str, body: dict):
    return store.prepare(sid, reader_text=body.get("reader_text", ""),
                         participants=body.get("participants", []),
                         kind=body.get("kind", "turn"),
                         request_id=body.get("request_id"), story_plan=body.get("story_plan"))


@app.get("/v2/novels/{sid}/chunks/{packet_id}/{index}")
def read_chunk(sid: str, packet_id: str, index: int):
    return store.read_chunk(sid, packet_id, index)


@app.get("/v2/scene-review-manifest")
def review_manifest():
    return store.scene_review_manifest()


@app.post("/v2/novels/{sid}/review")
def review(sid: str, body: dict):
    return store.review_scene(
        sid, body["expected_version"], body["packet_id"], body["request_id"],
        body["skin_sha256"], body["narration"], body["checks"],
        body.get("rule_checks"))


@app.post("/v2/novels/{sid}/turns")
def commit(sid: str, body: dict):
    return store.commit(sid, body)


@app.post("/v2/novels/{sid}/rollback")
def rollback(sid: str, body: dict):
    return store.rollback(sid, body["expected_version"], body["request_id"],
                          body["reason"])


@app.post("/v2/novels/{sid}/audit")
def audit(sid: str, body: dict):
    return store.audit(sid, body["expected_version"], body["packet_id"],
                       body["chunk_hashes"], body["report"],
                       body.get("corrections"))


@app.get("/v2/novels/{sid}/history")
def history(sid: str, start: int = 1, end: int | None = None, query: str = ""):
    return {"scenes": store.history(sid, start=start, end=end, query=query)}


# Register REST routes before mounting the V2 MCP adapter.
app.mount("/", mcp_app)
