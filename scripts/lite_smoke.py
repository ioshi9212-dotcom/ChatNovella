"""Run a real local TCP server/client smoke test in isolated temporary storage."""
from pathlib import Path
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
def main():
    with tempfile.TemporaryDirectory(prefix='lite-smoke-') as temp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}';token=secrets.token_urlsafe(32)
        env={**os.environ,'CHATNOVELLA_LITE_DATA_DIR':temp,'CHATNOVELLA_LITE_API_TOKEN':token,
             'CHATNOVELLA_LITE_PUBLIC_HOST':'localhost','CHATNOVELLA_LITE_PUBLIC_URL':base}
        log=open(Path(temp)/'server.log','w+')
        proc=subprocess.Popen([sys.executable,'-m','uvicorn','chatnovella_lite.server:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
        def call(path,body=None):
            req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body,ensure_ascii=False).encode(),
                headers={'Content-Type':'application/json','Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'})
            with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)
        try:
            for _ in range(100):
                try:
                    call('/healthz');break
                except urllib.error.URLError:
                    if proc.poll() is not None: raise RuntimeError('Server exited')
                    time.sleep(.1)
            else: raise RuntimeError('Server startup timeout')
            schema=call('/openapi.json');assert len(schema['paths'])==12
            assert schema['servers']==[{'url':base}]
            s=call('/sessions',json.loads((ROOT/'docs/lite/example-session.json').read_text()));sid=s['session_id']
            call('/sessions/'+sid+'/approve',{'expected_version':1,'canon_hash':s['canon_hash'],'reader_confirmation':'Утверждаю тестовую анкету.'})
            p=call('/sessions/'+sid+'/context',{'reader_text':'Попросить Рэя проверить шкаф.','participants':['NPC_001']})
            for i in range(p['chunk_count']):call(f'/sessions/{sid}/chunks/{p["packet_id"]}/{i}')
            scene='**Рина** — Рэй, посмотри шкаф.\n\n**Рэй** — Если там опять твои конспекты, разбирать будешь сама.\n\nОн потянул ручку. За дверцей лежал ключ с биркой «Архив». Рэй поднял его и повернул биркой к Рине.\n\n**Рина** — Вот теперь можешь считать, что я попросила удачно.'
            d={'request_id':'tcp-smoke-1','expected_version':p['version'],'packet_id':p['packet_id'],'chunk_hashes':p['chunk_hashes'],'reader_text':'Попросить Рэя проверить шкаф.','narration':scene,'participants':['NPC_001'],'knowledge_review':{'NPC_001':'none'},'state_patch':{'objects':['открытый шкаф','ключ с биркой Архив в руке Рэя']},'events':[{'date':'2026-09-06','participants':['POV_001','NPC_001'],'event':'Найден ключ от архива.','consequence':'Можно попасть в архив.','significance':'revelation','significance_reason':'Новый ключ открывает ранее недоступную сюжетную локацию.'}]}
            a=call('/sessions/'+sid+'/turns',d);assert a['saved'] and a['narration']==scene
            assert call('/sessions/'+sid+'/turns',d)==a
            call('/sessions/'+sid+'/rollback',{'expected_version':a['version'],'request_id':d['request_id'],'reason':'Проверка восстановления.'})
            assert call('/sessions/'+sid)['turn']==0
            result=call('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
            names=[t['name'] for t in result['result']['tools']]
            assert 'save_lite_turn' in names
            assert len(names)==12
            print(json.dumps({'tcp_http_smoke':'PASS','openapi_paths':len(schema['paths']),'mcp_tools':len(names),'saved_exact_scene':True,'idempotent':True,'rollback':True}))
        except Exception:
            log.flush();log.seek(0);print(log.read(),file=sys.stderr)
            raise
        finally:
            proc.terminate();proc.wait(timeout=10);log.close()

if __name__=='__main__': main()
