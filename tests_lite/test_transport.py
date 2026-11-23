import asyncio
from concurrent.futures import ThreadPoolExecutor, wait
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from chatnovella_lite.store import Store
from test_store import REG,relations,state,BASE

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        os.environ['CHATNOVELLA_LITE_DATA_DIR']=self.tmp.name
        from chatnovella_lite import server
        import importlib
        server=importlib.reload(server)
        self.server=server
        self.patch=patch.object(server,'store',Store(self.tmp.name,BASE));self.patch.start()
    def tearDown(self):
        self.patch.stop();self.tmp.cleanup()
    def test_actions_full_protocol_and_mcp_discovery(self):
        with TestClient(self.server.app) as c:
            r=c.post('/sessions',json={'original_text':'Полный канон Рины.','novel_text':'Полный канон Рины.',
                                     'registry':REG,'relationships':relations(),'state':state()})
            self.assertEqual(r.status_code,200,r.text)
            session=r.json();sid=session['session_id']
            self.assertEqual(c.post(f'/sessions/{sid}/approve',json={'expected_version':1,'canon_hash':session['canon_hash'],'reader_confirmation':'Да, утверждаю'}).status_code,200)
            p=c.post(f'/sessions/{sid}/context',json={'reader_text':'Открой шкаф.','participants':['NPC_001']}).json()
            for i in range(p['chunk_count']):
                self.assertEqual(c.get(f'/sessions/{sid}/chunks/{p["packet_id"]}/{i}').status_code,200)
            d={'request_id':'actions-1','expected_version':p['version'],'packet_id':p['packet_id'],'chunk_hashes':p['chunk_hashes'],
               'reader_text':'Открой шкаф.','narration':'**Рэй** — Посмотрим.\n\nОн открыл шкаф и положил ключ на стол.',
               'participants':['NPC_001'],'knowledge_review':{'POV_001':'none','NPC_001':'none'},'state_patch':{'objects':['открытый шкаф','ключ на столе']},'no_event_reason':'Только осмотр мебели.'}
            r=c.post(f'/sessions/{sid}/turns',json=d)
            self.assertEqual(r.status_code,200,r.text)
            self.assertTrue(r.json()['saved'])
            self.assertEqual(r.json()['narration'],d['narration'])
            self.assertEqual(c.post(f'/sessions/{sid}/turns',json=d).json(),r.json())
            saved=r.json()
            self.assertEqual(c.get('/sessions/CN_legacy').status_code,409)
            schema=c.get('/openapi.json').json()
            self.assertIn('Turn',schema['components']['schemas'])
            self.assertIn('/sessions/{session_id}/setup',schema['paths'])
            h={'accept':'application/json, text/event-stream'}
            r=c.post('/mcp',headers=h,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
            self.assertEqual(r.status_code,200,r.text)
            retry=c.post('/mcp',headers=h,json={
                'jsonrpc':'2.0','id':3,'method':'tools/call',
                'params':{'name':'save_lite_turn','arguments':{'session_id':sid,'turn':d}}})
            self.assertEqual(retry.status_code,200,retry.text)
            self.assertEqual(retry.json()['result']['structuredContent'],saved)
            r=c.post('/mcp',headers=h,json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
            self.assertEqual(r.status_code,200,r.text)
            names={t['name'] for t in r.json()['result']['tools']}
            self.assertIn('save_lite_turn',names)
            self.assertIn('update_lite_setup',names)
            self.assertFalse(any('review' in n or 'plan' in n for n in names))
    def test_auth_and_invalid_input(self):
        with patch.object(self.server,'TOKEN','x'*32),TestClient(self.server.app) as c:
            self.assertEqual(c.get('/sessions').status_code,401)
            self.assertEqual(c.get('/sessions',headers={'authorization':'Bearer '+'x'*32}).status_code,200)
            self.assertEqual(c.get('/healthz').status_code,200)
            self.assertEqual(c.post('/sessions',headers={'authorization':'Bearer '+'x'*32},json={}).status_code,422)
    def test_waiting_mcp_file_lock_keeps_health_and_discovery_responsive(self):
        session=self.server.store.create('Канон.','Канон.',REG,relations(),state())
        sid=session['session_id']
        status=self.server.store.status
        entered=threading.Event()
        def observed_status(session_id):
            entered.set()
            return status(session_id)
        headers={'accept':'application/json, text/event-stream'}
        with TestClient(self.server.app) as c,patch.object(self.server.store,'status',side_effect=observed_status),ThreadPoolExecutor(max_workers=3) as pool:
            fd=os.open(self.server.store.path(sid),os.O_RDONLY | os.O_DIRECTORY)
            fcntl.flock(fd,fcntl.LOCK_EX)
            try:
                pending=pool.submit(c.post,'/mcp',headers=headers,json={
                    'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':'get_lite_status','arguments':{'session_id':sid}}})
                self.assertTrue(entered.wait(5),'MCP status call did not start')
                health=pool.submit(c.get,'/healthz')
                discovery=pool.submit(c.post,'/mcp',headers=headers,json={
                    'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
                done,_=wait((health,discovery),timeout=1)
                responsive=len(done)==2
                self.assertFalse(pending.done(),'Status must wait for the session lock')
            finally:
                fcntl.flock(fd,fcntl.LOCK_UN)
                os.close(fd)
            result=pending.result(timeout=5)
            self.assertEqual(result.status_code,200,result.text)
            self.assertFalse(result.json()['result'].get('isError',False),result.text)
            self.assertEqual(health.result(timeout=5).status_code,200)
            self.assertEqual(discovery.result(timeout=5).status_code,200)
            self.assertTrue(responsive,'A waiting MCP file lock blocked health and tool discovery')
    def test_mcp_denies_untrusted_origin(self):
        with TestClient(self.server.app) as c:
            r=c.post('/mcp',headers={'origin':'https://untrusted.example','accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
            self.assertEqual(r.status_code,403)
    def test_production_config_inherits_host_token_and_uses_persistent_lite_root(self):
        import importlib
        from chatnovella_lite import server
        with patch.dict(os.environ,{'CHATNOVELLA_PUBLIC_HOST':'existing.amvera.tech','CHATNOVELLA_API_TOKEN':'y'*32},clear=True),patch('chatnovella_lite.store.Store') as factory:
            reloaded=importlib.reload(server)
            self.assertEqual(reloaded.HOST,'existing.amvera.tech')
            self.assertEqual(reloaded.TOKEN,'y'*32)
            self.assertEqual(factory.call_args.args[0],'/data/chatnovella-lite')
