"""Isolated API for ChatNovella V2. Does not import or expose Lite sessions."""
from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .engine import Engine, EngineError

app = FastAPI(title="ChatNovella V2", version="0.1.0")
store = Engine(os.getenv("CHATNOVELLA_V2_DATA_DIR", "./.novella_v2_data"))
TOKEN = os.getenv("CHATNOVELLA_V2_TOKEN", "")


@app.middleware("http")
async def require_token(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    # Fail closed. A deployed server without a token must not expose private novels.
    if len(TOKEN) < 24:
        return JSONResponse({"error": "CHATNOVELLA_V2_TOKEN must be configured"}, status_code=503)
    if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + TOKEN):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


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


@app.post("/v2/novels")
def create(body: dict):
    return store.create(
        original=body["original"], blocks=body["blocks"],
        names=body["names"], genre_answers=body.get("genre_answers"),
        relationships=body.get("relationships"))


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


@app.post("/v2/novels/{sid}/prepare")
def prepare(sid: str, body: dict):
    return store.prepare(sid, reader_text=body.get("reader_text", ""),
                         participants=body.get("participants", []),
                         kind=body.get("kind", "turn"),
                         request_id=body.get("request_id"))


@app.get("/v2/novels/{sid}/chunks/{packet_id}/{index}")
def read_chunk(sid: str, packet_id: str, index: int):
    return store.read_chunk(sid, packet_id, index)


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
