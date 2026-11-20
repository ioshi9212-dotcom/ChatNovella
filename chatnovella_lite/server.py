"""Separate test service: typed Actions REST + Streamable HTTP MCP.

Run uvicorn chatnovella_lite.server:app --host 127.0.0.1 --port 8081.
No production URL or production data directory is inherited.
"""
from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from pydantic import ValidationError
from .models import Model, Turn, State, Relationships
from .store import Store, Error

BASE=Path(__file__).resolve().parents[1]
TOKEN=os.getenv('CHATNOVELLA_LITE_API_TOKEN',os.getenv('CHATNOVELLA_API_TOKEN',''))
HOST=os.getenv('CHATNOVELLA_LITE_PUBLIC_HOST',os.getenv('CHATNOVELLA_PUBLIC_HOST','chatnovella-yumikofv.waw0.amvera.tech')).strip().lower()
if TOKEN and len(TOKEN)<24: raise RuntimeError('Lite token must have at least 24 characters')
store=Store(os.getenv('CHATNOVELLA_LITE_DATA_DIR','/data/chatnovella-lite'),BASE)
mcp=FastMCP('ChatNovella Lite',instructions=(BASE/'docs/lite/INSTRUCTIONS.md').read_text(),
            stateless_http=True,json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                allowed_hosts=[HOST,HOST+':*','localhost','localhost:*','127.0.0.1','127.0.0.1:*','testserver'],
                allowed_origins=['https://'+HOST,'https://chatgpt.com','http://localhost:*','http://127.0.0.1:*']))

# The same typed calls are exposed to MCP and Actions; no plans/review gate.
@mcp.tool()
def list_lite_sessions() -> list[dict[str,Any]]:
    """List only Lite sessions in this test service."""
    return store.list_sessions()

@mcp.tool()
def create_lite_session(original_text:str,novel_text:str,registry:dict[str,str],relationships:Relationships,state:State) -> dict[str,Any]:
    """Create proposed setup. Do not activate or write a first scene. Preserve original verbatim."""
    return store.create(original_text,novel_text,registry,relationships.model_dump(),state.model_dump())

@mcp.tool()
def get_lite_status(session_id:str) -> dict[str,Any]:
    """Return exact version, canon checksum, stable IDs and due audits."""
    return store.status(session_id)

@mcp.tool()
def approve_lite_canon(session_id:str,expected_version:int,canon_hash:str,reader_confirmation:str,approved_changes:str='') -> dict[str,Any]:
    """Only after explicit reader approval of full proposed canon and starting metrics. This does not start play."""
    return store.approve(session_id,expected_version,canon_hash,reader_confirmation,approved_changes)

@mcp.tool()
def amend_lite_canon(session_id:str,expected_version:int,novel_text:str,reader_confirmation:str,reason:str,registry_additions:dict[str,str]|None=None) -> dict[str,Any]:
    """Only reader-authorized canon amendment; preserve previous canon in operation archive."""
    return store.amend_canon(session_id,expected_version,novel_text,reader_confirmation,reason,registry_additions)

@mcp.tool()
def prepare_lite_context(session_id:str,reader_text:str,participants:list[str],kind:str='turn') -> dict[str,Any]:
    """Prepare full canon, two engine files, relations/state/timeline and 3 scenes. kind=canon for setup; kind=audit at checkpoints. Select entering NPC IDs before writing their scenes."""
    return store.prepare(session_id,reader_text,participants,kind)

@mcp.tool()
def read_lite_chunk(session_id:str,packet_id:str,index:int) -> dict[str,Any]:
    """Read zero-based lossless chunk; verify SHA256 and read all chunks."""
    return store.read_chunk(session_id,packet_id,index)

@mcp.tool()
def save_lite_turn(session_id:str,turn:Turn) -> dict[str,Any]:
    """Atomically persist final scene plus state and justified memory/relationship/events. Publish returned narration only after saved=true. Reuse request_id on retry."""
    return store.commit(session_id,**turn.model_dump())

@mcp.tool()
def finish_lite_audit(session_id:str,expected_version:int,packet_id:str,chunk_hashes:list[str],report:dict[str,str],corrections:dict[str,dict[str,str]]) -> dict[str,Any]:
    """After reading audit chunks, submit exact source quotations per domain and justified corrections. No canon/history rewrites."""
    return store.finish_audit(session_id,expected_version,packet_id,chunk_hashes,report,corrections)

@mcp.tool()
def rollback_lite_turn(session_id:str,expected_version:int,request_id:str,reason:str) -> dict[str,Any]:
    """On reader request revert exact last turn and affected data. Preserve original scene in append-only history."""
    return store.rollback(session_id,expected_version,request_id,reason)

@mcp.tool()
def search_lite_history(session_id:str,start:int=1,end:int|None=None,query:str='') -> dict[str,Any]:
    """Retrieve complete active scenes by turn or exact text; result uses read_lite_chunk for large archives."""
    return store.read_history(session_id,start,end,query)

mcp_app=mcp.streamable_http_app()
@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield
app=FastAPI(title='ChatNovella Lite',version='0.1.0',lifespan=lifespan,
            description='Isolated test engine. Full canon, selected NPC knowledge, atomic turns, audits 15/60.')

@app.middleware('http')
async def bearer(request,call_next):
    if TOKEN and request.url.path not in ('/healthz','/openapi.json'):
        if not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+TOKEN):
            return JSONResponse({'detail':'Unauthorized'},status_code=401)
    return await call_next(request)

@app.exception_handler(Error)
async def store_error(request,exc):
    return JSONResponse({'detail':str(exc)},status_code=409)

@app.exception_handler(ValidationError)
async def validation_error(request,exc):
    return JSONResponse({'detail':str(exc)},status_code=422)

class Create(Model):
    original_text:str
    novel_text:str
    registry:dict[str,str]
    relationships:Relationships
    state:State
class Approval(Model):
    expected_version:int
    canon_hash:str
    reader_confirmation:str
    approved_changes:str=''
class Amendment(Model):
    expected_version:int
    novel_text:str
    reader_confirmation:str
    reason:str
    registry_additions:dict[str,str] = {}
class Context(Model):
    reader_text:str
    participants:list[str]
    kind:str='turn'
class Audit(Model):
    expected_version:int
    packet_id:str
    chunk_hashes:list[str]
    report:dict[str,str]
    corrections:dict[str,dict[str,str]]={}
class Rollback(Model):
    expected_version:int
    request_id:str
    reason:str

@app.get('/healthz',operation_id='liteHealth')
def health(): return {'status':'ok','engine':'lite','version':'0.1.0'}
@app.get('/sessions',operation_id='listLiteSessions')
def sessions(): return store.list_sessions()
@app.post('/sessions',operation_id='createLiteSession')
def create(body:Create): return store.create(**body.model_dump())
@app.get('/sessions/{session_id}',operation_id='getLiteStatus')
def status(session_id:str): return store.status(session_id)
@app.post('/sessions/{session_id}/approve',operation_id='approveLiteCanon')
def approve(session_id:str,body:Approval): return store.approve(session_id,**body.model_dump())
@app.post('/sessions/{session_id}/canon',operation_id='amendLiteCanon')
def amend(session_id:str,body:Amendment): return store.amend_canon(session_id,**body.model_dump())
@app.post('/sessions/{session_id}/context',operation_id='prepareLiteContext')
def prepare(session_id:str,body:Context): return store.prepare(session_id,**body.model_dump())
@app.get('/sessions/{session_id}/chunks/{packet_id}/{index}',operation_id='readLiteChunk')
def chunk(session_id:str,packet_id:str,index:int): return store.read_chunk(session_id,packet_id,index)
@app.post('/sessions/{session_id}/turns',operation_id='saveLiteTurn')
def commit(session_id:str,body:Turn): return store.commit(session_id,**body.model_dump())
@app.post('/sessions/{session_id}/audit',operation_id='finishLiteAudit')
def audit(session_id:str,body:Audit): return store.finish_audit(session_id,**body.model_dump())
@app.post('/sessions/{session_id}/rollback',operation_id='rollbackLiteTurn')
def rollback(session_id:str,body:Rollback): return store.rollback(session_id,**body.model_dump())
@app.get('/sessions/{session_id}/history',operation_id='searchLiteHistory')
def history(session_id:str,start:int=1,end:int|None=None,query:str=''): return store.read_history(session_id,start,end,query)

# Explicit auth schema for importing generated OpenAPI into Actions.
_original_openapi=app.openapi
def openapi():
    schema=_original_openapi()
    schema.setdefault('components',{}).setdefault('securitySchemes',{})['BearerAuth']={'type':'http','scheme':'bearer'}
    if TOKEN: schema['security']=[{'BearerAuth':[]}]
    url=os.getenv('CHATNOVELLA_LITE_PUBLIC_URL','https://'+HOST if HOST!='localhost' else '')
    if url: schema['servers']=[{'url':url.rstrip('/')}]
    return schema
app.openapi=openapi

class Setup(Model):
    expected_version:int
    novel_text:str
    relationships:Relationships
    state:State

@mcp.tool()
def update_lite_setup(session_id:str,expected_version:int,novel_text:str,relationships:Relationships,state:State) -> dict[str,Any]:
    """Edit unapproved setup after reader corrections. Original and permanent registry remain intact."""
    return store.update_setup(session_id,expected_version,novel_text,relationships.model_dump(),state.model_dump())

# Register Actions routes before the root MCP mount.
@app.post('/sessions/{session_id}/setup',operation_id='updateLiteSetup')
def setup(session_id:str,body:Setup): return store.update_setup(session_id,**body.model_dump())
app.mount('/',mcp_app)
