"""Readable session files + one recoverable transaction in system/meta.json.

All readers use the same directory flock. A pending manifest is fsynced BEFORE
materializing files. Interrupted commits roll forward under the lock; caught
write failures restore the old image. No per-turn directories or database.
"""
from __future__ import annotations
import contextlib
from copy import deepcopy
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any
from .models import State, Relationships, RelationChange, Knowledge, Turn

SID = re.compile(r'^LITE_[a-f0-9]{32}$')
NPC = re.compile(r'^NPC_[0-9]{3,}$')
CHUNK_SIZE = 16000
# Reject only explicit epistemic assertions introduced by a NEW scene-state patch.
# Already-stored legacy state is not silently changed; it needs a reviewed repair.
_STATE_KNOWLEDGE = re.compile(
    r'(?iu)\b(?:не\s+зна(?:ет|ют|л[аи]?|ли)|зна(?:ет|ют|л[аи]?|ли)|'
    r'узна(?:л[аи]?|ли)|увидел(?:а|и)?|видел(?:а|и)?|'
    r'услышал(?:а|и)?|слышал(?:а|и)?|'
    r'помнит|помнят|помнил(?:а|и)?|известно\s+(?:ему|ей|им)|'
    r'осведомл[её]н(?:а|ы)?)\b'
)

def norm(text):
    return ' '.join(str(text).casefold().replace('ё', 'е').split())

def check_state_patch_scope(patch):
    """State is the current scene, not a second NPC memory store."""
    if not isinstance(patch, dict):
        raise Error('state_patch must be an object')
    def scan(value, field):
        if isinstance(value, str):
            if _STATE_KNOWLEDGE.search(value):
                raise Error(f'NPC knowledge found in state_patch.{field}; use knowledge for its owner ID')
        elif isinstance(value, list):
            for item in value: scan(item, field)
        elif isinstance(value, dict):
            for key, item in value.items(): scan(item, field + '.' + key)
    # Inspect every supplied field, including apparently harmless item/position
    # descriptions: the previous leak came through many different state slots.
    scan(patch, 'state_patch')

class Error(ValueError):
    pass

def dump(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)

def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def document(title, obj):
    return '# ' + title + '\n\n```json\n' + dump(obj) + '\n```\n'

def parse_document(text):
    return json.loads(text.split('```json\n', 1)[1].rsplit('\n```', 1)[0])

def merge(old, patch):
    result = deepcopy(old)
    for key, val in patch.items():
        result[key] = merge(result[key], val) if isinstance(val, dict) and isinstance(result.get(key), dict) else deepcopy(val)
    return result

def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.lite-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

class Store:
    def __init__(self, root, engine_root):
        self.root, self.engine = Path(root), Path(engine_root)
        # An empty explicit test root is required. Never default to legacy storage.
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / 'sessions').exists() or any(self.root.glob('CN_*')):
            raise Error('Use a separate Lite data directory, not legacy storage')

    def path(self, sid):
        if not isinstance(sid, str) or not SID.fullmatch(sid):
            raise Error('Invalid Lite session ID')
        path = self.root / sid
        if not path.is_dir() or path.is_symlink():
            raise Error('Session not found')
        return path

    @contextlib.contextmanager
    def locked(self, sid):
        path = self.path(sid)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            meta = json.loads((path / 'system/meta.json').read_bytes().decode('utf-8'))
            if 'pending' in meta:
                pending = meta['pending']
                self.materialize(path, pending['after'])
                meta = pending['meta']
                atomic(path / 'system/meta.json', dump(meta))
            self.verify(path, meta)
            yield path, meta
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def verify(self, path, meta):
        if meta.get('approved'):
            if digest((path/'novel.md').read_bytes().decode('utf-8')) != meta['canon_hash']:
                raise Error('Canon checksum mismatch; restore data before continuing')
        if digest((path/'original.md').read_bytes().decode('utf-8')) != meta['original_hash']:
            raise Error('Original checksum mismatch')

    def materialize(self, path, files):
        for name, text in files.items():
            self.safe(name)
            if text is None:
                (path/name).unlink(missing_ok=True)
                if (path/name).parent.exists():
                    sync_dir((path/name).parent)
            else:
                atomic(path/name, text)

    @staticmethod
    def safe(name):
        if name not in {'original.md','novel.md','relationships.md','state.md','timeline.md','history.jsonl'} and not re.fullmatch(r'knowledge/NPC_[0-9]{3,}\.md',name):
            raise Error('Invalid file path')

    def transaction(self, path, meta, files, new):
        before = {name: (path/name).read_bytes().decode('utf-8') if (path/name).exists() else None for name in files}
        atomic(path/'system/meta.json', dump({**meta,'pending':{'after':files,'meta':new}}))
        try:
            self.materialize(path, files)
            atomic(path/'system/meta.json', dump(new))
        except Exception:
            # Bypass fault-injected materialize hook for rollback. If the device
            # itself remains unavailable, durable pending will recover on restart.
            for name,text in before.items():
                if text is None:
                    (path/name).unlink(missing_ok=True)
                else:
                    atomic(path/name,text)
            atomic(path/'system/meta.json',dump(meta))
            raise

    @staticmethod
    def version(meta, expected):
        if type(expected) is not int or expected != meta['version']:
            raise Error('Stale version; reload context')

    @staticmethod
    def status_of(meta):
        return {key:meta[key] for key in ('session_id','version','turn','approved','canon_hash','registry','audit_due')}

    def status(self, sid):
        with self.locked(sid) as (_,meta):
            return self.status_of(meta)

    def list_sessions(self):
        return [self.status(p.name) for p in sorted(self.root.glob('LITE_*')) if p.is_dir()]

    def create(self, original_text, novel_text, registry, relationships, state):
        if not isinstance(original_text,str) or not original_text.strip() or not novel_text.strip():
            raise Error('Original and full proposed canon are required')
        if not isinstance(registry,dict) or set(k for k in registry if k.startswith('POV_')) != {'POV_001'}:
            raise Error('Registry requires exactly POV_001')
        if not all((key=='POV_001' or NPC.fullmatch(key)) and isinstance(name,str) and name.strip() for key,name in registry.items()):
            raise Error('Invalid permanent character registry')
        state = State.model_validate(state).model_dump()
        check_state_patch_scope(state)
        if state['turn'] != 0:
            raise Error('Setup turn must be zero')
        self.validate_state(state,registry)
        rel = Relationships.model_validate(relationships).model_dump()
        self.validate_relations(rel,registry)
        sid='LITE_'+secrets.token_hex(16)
        # Hidden staging directory is never listed. Rename publishes complete setup.
        tmp=Path(tempfile.mkdtemp(prefix='.create-',dir=self.root))
        try:
            files={'original.md':original_text,'novel.md':self.canon(novel_text,registry),
                   'relationships.md':document('Отношения',rel),'state.md':document('Состояние',state),
                   'timeline.md':'# Хронология игровых событий\n','history.jsonl':''}
            # Personal acquisition logs exist for every permanent NPC from day one.
            # The canonical biography remains only in novel.md.
            for cid in registry:
                if NPC.fullmatch(cid):
                    files[f'knowledge/{cid}.md']=document('Приобретённые знания',[])
            self.materialize(tmp,files)
            meta={'session_id':sid,'version':1,'turn':0,'approved':False,'canon_hash':digest(files['novel.md']),
                  'original_hash':digest(original_text),'registry':registry,'audit_due':[],
                  'packets':{},'requests':{},'operations':[],'last_undo':None}
            atomic(tmp/'system/meta.json',dump(meta))
            os.rename(tmp,self.root/sid)
            sync_dir(self.root)
        except Exception:
            # Keep an incomplete hidden creation available for diagnosis, never load it.
            raise
        return self.status_of(meta)

    @staticmethod
    def canon(text, registry):
        return text+'\n\n<!-- LITE_REGISTRY -->\n# Постоянные ID\n'+''.join(f'- {cid}: {name}\n' for cid,name in registry.items())

    def approve(self, sid, expected_version, canon_hash, reader_confirmation, approved_changes=''):
        with self.locked(sid) as (path,meta):
            self.version(meta,expected_version)
            if meta['approved'] or not isinstance(reader_confirmation,str) or not reader_confirmation.strip():
                raise Error('Explicit reader confirmation of the complete proposed canon is required')
            if canon_hash!=meta['canon_hash']:
                raise Error('Approve the exact displayed canon hash')
            canon=(path/'novel.md').read_bytes().decode('utf-8').split('\n\n<!-- LITE_REGISTRY -->')[0]
            if canon!=(path/'original.md').read_bytes().decode('utf-8') and not approved_changes.strip():
                raise Error('Document reader-approved changes from verbatim original')
            new=deepcopy(meta)
            new.update(approved=True,version=meta['version']+1)
            new['operations'].append({'kind':'approval','confirmation':reader_confirmation,'changes':approved_changes,'canon_hash':canon_hash})
            self.transaction(path,meta,{},new)
            return self.status_of(new)

    def update_setup(self,sid,expected_version,novel_text,relationships,state):
        with self.locked(sid) as (path,meta):
            self.version(meta,expected_version)
            if meta['approved']: raise Error('Setup is locked; use an explicitly approved canon amendment')
            if not novel_text.strip(): raise Error('Full proposed canon required')
            state=State.model_validate(state).model_dump()
            check_state_patch_scope(state)
            if state['turn']!=0: raise Error('Setup turn must be zero')
            self.validate_state(state,meta['registry'])
            rel=Relationships.model_validate(relationships).model_dump()
            self.validate_relations(rel,meta['registry'])
            text=self.canon(novel_text,meta['registry'])
            new=deepcopy(meta)
            new.update(version=meta['version']+1,canon_hash=digest(text),packets={})
            self.transaction(path,meta,{'novel.md':text,'relationships.md':document('Отношения',rel),'state.md':document('Состояние',state)},new)
            return self.status_of(new)

    def amend_canon(self,sid,expected_version,novel_text,reader_confirmation,reason,registry_additions=None):
        with self.locked(sid) as (path,meta):
            self.version(meta,expected_version)
            if not meta['approved'] or not reader_confirmation.strip() or not reason.strip() or not novel_text.strip():
                raise Error('Explicit reader permission and reason required')
            registry=deepcopy(meta['registry'])
            for cid,name in (registry_additions or {}).items():
                if cid in registry or not NPC.fullmatch(cid) or not isinstance(name,str) or not name.strip():
                    raise Error('Add only new stable NPC IDs; existing IDs cannot be reassigned')
                registry[cid]=name
            text=self.canon(novel_text,registry)
            new=deepcopy(meta)
            new.update(version=meta['version']+1,canon_hash=digest(text),packets={},registry=registry)
            new['operations'].append({'kind':'canon_amendment','old_registry':meta['registry'],'old_text':(path/'novel.md').read_bytes().decode('utf-8'),'new_hash':new['canon_hash'],'reason':reason,'confirmation':reader_confirmation})
            files={'novel.md':text}
            for cid in (registry_additions or {}):
                files[f'knowledge/{cid}.md']=document('Приобретённые знания',[])
            self.transaction(path,meta,files,new)
            return self.status_of(new)

    @staticmethod
    def validate_state(state,registry):
        ids=set(state['present'])|set(state['remote'])
        if not ids<=set(registry)-{'POV_001'} or set(state['present'])&set(state['remote']):
            raise Error('Physical/remote participants must be distinct registered NPC IDs')
        if state['datetime'] is not None:
            try: datetime.fromisoformat(state['datetime'])
            except (ValueError,TypeError): raise Error('Use ISO world datetime or null when unknown')

    @staticmethod
    def validate_relations(rel,registry):
        ids=set(registry)-{'POV_001'}
        for cid,metrics in rel['npc_to_pov'].items():
            if cid not in ids: raise Error('Numeric relations require a registered NPC')
            for label,value in metrics.items():
                if not label.strip() or type(value) is not int or not 0<=value<=100:
                    raise Error('Named NPC→POV metrics must be integers 0..100')
        for source,targets in rel['npc_to_npc'].items():
            if source not in ids: raise Error('Unknown relation source')
            for target,value in targets.items():
                if target not in ids or source==target or not isinstance(value,str) or not value.strip():
                    raise Error('NPC→NPC is directed qualitative text, not numbers')
                if re.fullmatch(r'\s*\d+(?:\s*/\s*[+-]?\d+)?\s*',value):
                    raise Error('NPC→NPC must use words')

    def relationship_changes(self,rel,changes,registry):
        result=deepcopy(rel)
        seen=set()
        for raw in changes:
            change=RelationChange.model_validate(raw).model_dump()
            src,target,metric=change['source'],change['target'],change['metric']
            key=(src,target,metric)
            if key in seen: raise Error('Duplicate relation update')
            seen.add(key)
            if src not in registry or src=='POV_001': raise Error('Unknown NPC relation source')
            if target=='POV_001':
                if not metric or not metric.strip(): raise Error('Metric name required')
                bucket=result['npc_to_pov'].setdefault(src,{})
                previous=bucket.get(metric)
                value=change['value']
                if value is None:
                    if previous!=0: raise Error('Only a zero-valued metric may be removed')
                    del bucket[metric]
                else:
                    if type(value) is not int or not 0<=value<=100: raise Error('Metric must be 0..100')
                    if previous is not None and abs(value-previous)>3 and not change['significant']:
                        raise Error('Delta >3 requires a significant event and reason')
                    bucket[metric]=value
            else:
                if metric is not None or not isinstance(change['value'],str): raise Error('NPC→NPC accepts words only')
                result['npc_to_npc'].setdefault(src,{})[target]=change['value']
        self.validate_relations(result,registry)
        return result

    @staticmethod
    def history(path):
        return [json.loads(line) for line in (path/'history.jsonl').read_bytes().decode('utf-8').splitlines() if line]

    @staticmethod
    def active_history(path):
        active=[]
        for row in Store.history(path):
            if row['kind']=='turn': active.append(row)
            elif row['kind']=='rollback':
                if not active or active[-1]['request_id']!=row['reverted_request_id']: raise Error('Invalid rollback history')
                active.pop()
        return active

    def prepare(self,sid,reader_text,participants,kind='turn'):
        with self.locked(sid) as (path,meta):
            if not meta['approved'] and kind!='canon': raise Error('Approve full canon before preparing a scene')
            if kind not in ('turn','audit','canon'): raise Error('Invalid context kind')
            if kind=='turn' and meta['audit_due']: raise Error('Complete due audit before the next turn')
            if kind=='audit' and not meta['audit_due']: raise Error('No audit due')
            if len(participants)!=len(set(participants)) or not set(participants)<=set(meta['registry'])-{'POV_001'}:
                raise Error('Select unique registered NPC IDs')
            state=parse_document((path/'state.md').read_bytes().decode('utf-8'))
            if kind=='turn': participants=sorted(set(participants)|set(state['present'])|set(state['remote']))
            rows=self.active_history(path)
            if kind=='audit':
                scope=rows[-15:]
                participants=sorted(set(cid for row in scope for cid in row['participants']))
                affected=set(participants)
                for row in scope:
                    affected.update(entry['npc_id'] for entry in row.get('knowledge',[]))
                    affected.update(cid for event in row.get('events',[]) for cid in event['participants'] if cid!='POV_001')
                    affected.update(change['source'] for change in row.get('relations',[]))
                    affected.update(change['target'] for change in row.get('relations',[]) if change['target']!='POV_001')
                participants=sorted(affected)
                if 60 in meta['audit_due']: participants=sorted(set(meta['registry'])-{'POV_001'})
            else: scope=rows[-3:]
            pieces={name:(self.engine/name).read_bytes().decode('utf-8') for name in ('engine/rules.md','engine/skin_builder.md')}
            pieces.update({name:(path/name).read_bytes().decode('utf-8') for name in ('novel.md','relationships.md','state.md','timeline.md')})
            if kind=='canon': pieces['original.md']=(path/'original.md').read_bytes().decode('utf-8')
            pieces['recent_scenes.json']=dump([{'turn':r['turn'],'reader_text':r['reader_text'],'narration':r['narration']} for r in scope])
            for cid in participants:
                name=f'knowledge/{cid}.md'
                if (path/name).exists(): pieces[name]=(path/name).read_bytes().decode('utf-8')
            payload=dump({'kind':kind,'reader_text':reader_text,'participants':participants,'audit_due':meta['audit_due'],'files':pieces})
            chunks=[payload[i:i+CHUNK_SIZE] for i in range(0,len(payload),CHUNK_SIZE)]
            packet_id=secrets.token_hex(16)
            packet={'version':meta['version'],'kind':kind,'reader_text':reader_text,'participants':participants,
                    'payload':payload,'chunk_size':CHUNK_SIZE,'hashes':[digest(c) for c in chunks],'delivered':[],'files':{k:digest(v) for k,v in pieces.items()}}
            # Keep only latest packets; bounded metadata, never delete scene history.
            meta['packets']={k:v for k,v in list(meta['packets'].items())[-3:] if v['version']==meta['version']}
            meta['packets'][packet_id]=packet
            atomic(path/'system/meta.json',dump(meta))
            return {'packet_id':packet_id,'version':meta['version'],'chunk_count':len(chunks),'sha256':digest(payload),
                    'chunk_size':CHUNK_SIZE,'chunk_hashes':packet['hashes'],'file_hashes':packet['files'],'characters':len(payload),'participants':participants,'audit_due':meta['audit_due']}

    def read_chunk(self,sid,packet_id,index):
        with self.locked(sid) as (path,meta):
            packet=meta['packets'].get(packet_id)
            if not packet or packet['version']!=meta['version']: raise Error('Expired packet; prepare again')
            if type(index) is not int or not 0<=index<len(packet['hashes']): raise Error('Invalid chunk index')
            # Already-issued packets retain their original boundaries after rollout.
            size=packet.get('chunk_size',12000)
            text=packet['payload'][index*size:(index+1)*size]
            packet['delivered']=sorted(set(packet['delivered'])|{index})
            atomic(path/'system/meta.json',dump(meta))
            return {'index':index,'text':text,'sha256':digest(text),'version':meta['version']}

    @staticmethod
    def receipt(meta,packet_id,hashes,kind):
        packet=meta['packets'].get(packet_id)
        if not packet or packet['version']!=meta['version'] or packet['kind']!=kind: raise Error('Fresh context packet required')
        if hashes!=packet['hashes'] or packet['delivered']!=list(range(len(hashes))): raise Error('Read and verify ALL chunks before continuing')
        return packet

    def commit(self,sid,**payload):
        data=Turn.model_validate(payload).model_dump()
        with self.locked(sid) as (path,meta):
            # Idempotency is checked before stale version: a lost response may be retried.
            semantic={k:v for k,v in data.items() if k not in ('packet_id','chunk_hashes','expected_version')}
            request_hash=digest(dump(semantic))
            prior=meta['requests'].get(data['request_id'])
            if prior:
                if prior['hash']!=request_hash: raise Error('Request ID reused with different content')
                if prior.get('reverted'): raise Error('Request was rolled back; use a new request ID')
                row=next(row for row in self.history(path) if row.get('kind')=='turn' and row.get('request_id')==data['request_id'])
                return {**prior['result'],'narration':row['narration']}
            self.version(meta,data['expected_version'])
            if not meta['approved'] or meta['audit_due']: raise Error('Approval or audit required')
            packet=self.receipt(meta,data['packet_id'],data['chunk_hashes'],'turn')
            if packet['reader_text']!=data['reader_text']: raise Error('Reader input differs from prepared input')
            if not set(data['participants'])<=set(packet['participants']): raise Error('Load entering NPC knowledge before writing the scene')
            if len(data['participants'])!=len(set(data['participants'])): raise Error('Duplicate participant')
            # Catch knowledge smuggled into scene state, but never rewrite legacy
            # state automatically: historical repairs must be author-reviewed.
            check_state_patch_scope(data['state_patch'])
            owners={entry['npc_id'] for entry in data['knowledge']}
            reviewed=set(data['participants']) | owners
            if set(data['knowledge_review']) != reviewed:
                raise Error('knowledge_review must cover every participating NPC and offscreen knowledge owner')
            for cid in reviewed:
                if cid not in meta['registry'] or cid=='POV_001':
                    raise Error('Unknown knowledge-review owner')
                has_entries=cid in owners
                if (data['knowledge_review'][cid]=='recorded') != has_entries:
                    raise Error(f'Knowledge review for {cid} disagrees with personal-memory entries')
            before_state=parse_document((path/'state.md').read_bytes().decode('utf-8'))
            state=merge(before_state,data['state_patch'])
            # Membership dictionaries are full replacements when supplied;
            # individual POV fields merge, and all lists replace explicitly.
            for key in ('present','remote'):
                if key in data['state_patch']: state[key]=deepcopy(data['state_patch'][key])
            state['turn']=meta['turn']+1
            state=State.model_validate(state).model_dump()
            self.validate_state(state,meta['registry'])
            if before_state['datetime'] and state['datetime']:
                try:
                    if datetime.fromisoformat(state['datetime']) < datetime.fromisoformat(before_state['datetime']):
                        raise Error('World time cannot move backwards within an ordinary turn')
                except TypeError:
                    raise Error('Keep world datetime timezone format consistent')
            if not (set(state['present'])|set(state['remote']))<=set(data['participants']): raise Error('Present/remote NPCs must be participants')
            rel=parse_document((path/'relationships.md').read_bytes().decode('utf-8'))
            relations=self.relationship_changes(rel,data['relations'],meta['registry'])
            files={'state.md':document('Состояние',state),'relationships.md':document('Отношения',relations)}
            turn=state['turn']
            if not data['events'] and not data['no_event_reason'].strip(): raise Error('Provide events or explain why no significant event occurred')
            timeline=(path/'timeline.md').read_bytes().decode('utf-8')
            # Reject exact repeat milestones instead of endlessly appending them.
            previous_events=set()
            for fragment in re.findall(r'```json\n(.*?)\n```',timeline,re.S):
                try: prior_event=json.loads(fragment)
                except (ValueError,TypeError): continue
                if isinstance(prior_event,dict) and 'event' in prior_event and 'consequence' in prior_event:
                    previous_events.add((norm(prior_event['event']),norm(prior_event['consequence']),prior_event.get('date')))
            for event in data['events']:
                event_key=(norm(event['event']),norm(event['consequence']),event['date'])
                if event_key in previous_events:
                    raise Error('Duplicate timeline milestone; keep NPC observations in knowledge instead')
                previous_events.add(event_key)
                if not set(event['participants'])<=set(meta['registry']): raise Error('Unknown event participant')
                if event['date'] is not None:
                    try: datetime.fromisoformat(event['date'])
                    except (TypeError,ValueError): raise Error('Event date must be ISO or null')
                timeline+='\n'+document(f'Ход {turn}',{**event,'turn':turn})
            files['timeline.md']=timeline
            for entry in data['knowledge']:
                cid=entry['npc_id']
                if cid not in packet['participants']: raise Error('Load owner knowledge before adding knowledge')
                if not set(entry['source_ids'])<=set(meta['registry']): raise Error('Unknown knowledge source')
                if entry['source_kind']=='offscreen':
                    i=entry['event_index']
                    if i is None:
                        # Knowledge can be gained offscreen without inventing a
                        # WORLD milestone. Source must preserve a specific trace.
                        if len(entry['source'].strip()) < 25 or len(entry['evidence'].strip()) < 12 or entry['evidence'] not in entry['source']:
                            raise Error('Offscreen-only knowledge requires a concrete source containing its exact evidence')
                    else:
                        if type(i) is not int or not 0<=i<len(data['events']) or cid not in data['events'][i]['participants']:
                            raise Error('Offscreen chronology reference must involve the knowledge owner')
                        if entry['evidence'] not in dump(data['events'][i]): raise Error('Offscreen evidence absent from event')
                elif cid not in data['participants'] or entry['evidence'] not in data['narration']:
                    raise Error('Knowledge requires owner participation and a source quote from this scene')
                name=f'knowledge/{cid}.md'
                old=files.get(name,(path/name).read_bytes().decode('utf-8') if (path/name).exists() else document('Приобретённые знания',[]))
                entries=parse_document(old)
                key=(norm(entry['fact']),entry['status'],entry['source_kind'],
                     tuple(entry['source_ids']),norm(entry['source']),norm(entry['evidence']))
                if any((norm(row['fact']),row['status'],row['source_kind'],
                        tuple(row['source_ids']),norm(row['source']),norm(row['evidence']))==key for row in entries):
                    raise Error(f'Duplicate personal knowledge for {cid}; do not save the same acquisition twice')
                if entry['supersedes'] is not None and not 0<=entry['supersedes']<len(entries): raise Error('Invalid superseded knowledge index')
                entries.append({**entry,'turn':turn,'date':state['datetime']})
                files[name]=document('Приобретённые знания',entries)
            record={'kind':'turn','turn':turn,'version':meta['version']+1,**{k:v for k,v in data.items() if k not in ('expected_version','chunk_hashes')},
                    'state_before':before_state,'state_after':state,'relationships_before':rel,'relationships_after':relations}
            files['history.jsonl']=(path/'history.jsonl').read_bytes().decode('utf-8')+json.dumps(record,ensure_ascii=False)+'\n'
            new=deepcopy(meta)
            new.update(version=meta['version']+1,turn=turn,packets={},audit_due=([15,60] if turn%60==0 else [15]) if turn%15==0 else [])
            new['last_undo']={'files':{name:(path/name).read_bytes().decode('utf-8') if (path/name).exists() else None for name in files if name!='history.jsonl'},
                              'turn':meta['turn'],'audit_due':meta['audit_due'],'request_id':data['request_id']}
            result={'session_id':sid,'turn':turn,'version':new['version'],'saved':True,'narration':data['narration'],
                    'audit_due':new['audit_due'],'knowledge_written':len(data['knowledge']),
                    'knowledge_owners':sorted(owners),'timeline_events_written':len(data['events'])}
            new['requests'][data['request_id']]={'hash':request_hash,'result':{k:v for k,v in result.items() if k!='narration'}}
            self.transaction(path,meta,files,new)
            return result

    def rollback(self,sid,expected_version,request_id,reason):
        with self.locked(sid) as (path,meta):
            self.version(meta,expected_version)
            undo=meta.get('last_undo')
            if not undo or undo['request_id']!=request_id or not reason.strip(): raise Error('Only exact last turn can be rolled back, with a reason')
            files=deepcopy(undo['files'])
            files['history.jsonl']=(path/'history.jsonl').read_bytes().decode('utf-8')+json.dumps({'kind':'rollback','reverted_request_id':request_id,'turn':meta['turn'],'reason':reason,'version':meta['version']+1},ensure_ascii=False)+'\n'
            new=deepcopy(meta)
            new.update(version=meta['version']+1,turn=undo['turn'],audit_due=undo['audit_due'],packets={},last_undo=None)
            new['requests'][request_id]['reverted']=True
            self.transaction(path,meta,files,new)
            return self.status_of(new)

    def finish_audit(self,sid,expected_version,packet_id,chunk_hashes,report,corrections):
        with self.locked(sid) as (path,meta):
            self.version(meta,expected_version)
            packet=self.receipt(meta,packet_id,chunk_hashes,'audit')
            if not meta['audit_due']: raise Error('No audit due')
            required={'continuity','relationships','knowledge','time_state','timeline'}
            if 60 in meta['audit_due']: required|={'all_npc_knowledge','long_term'}
            if set(report)!=required or not all(isinstance(v,str) and v.strip() for v in report.values()): raise Error('Audit must cover each required domain with source evidence')
            # Evidence is explicit exact source quotation, not a ceremonial checkbox.
            source=json.loads(packet['payload'])['files']
            joined='\n'.join(source.values())
            joined+='\n'+'\n'.join(row['narration'] for row in json.loads(source['recent_scenes.json']))
            for text in report.values():
                if text not in joined: raise Error('Each audit domain requires a verbatim source excerpt')
            files={}
            for name,change in corrections.items():
                if name not in ('state.md','relationships.md','timeline.md') and not re.fullmatch(r'knowledge/NPC_[0-9]{3,}\.md',name): raise Error('Audit cannot change canon or history')
                if not isinstance(change,dict) or set(change)!={'before','after','reason'} or not change['reason'].strip(): raise Error('Audited correction needs exact before/after and reason')
                current=(path/name).read_bytes().decode('utf-8') if (path/name).exists() else ''
                if change['before']!=current: raise Error('Correction before-image mismatch')
                if name=='state.md':
                    st=State.model_validate(parse_document(change['after'])).model_dump()
                    self.validate_state(st,meta['registry'])
                    if st['turn']!=meta['turn']: raise Error('Audit cannot alter turn counter')
                elif name=='relationships.md': self.validate_relations(Relationships.model_validate(parse_document(change['after'])).model_dump(),meta['registry'])
                elif name.startswith('knowledge/'):
                    cid=Path(name).stem
                    if cid not in packet['participants']: raise Error('Knowledge owner outside audit scope')
                    old=parse_document(current) if current else []
                    after=parse_document(change['after'])
                    if after[:len(old)]!=old: raise Error('Append a sourced correction; preserve knowledge history')
                    for row in after[len(old):]:
                        entry=Knowledge.model_validate({k:v for k,v in row.items() if k not in ('turn','date')}).model_dump()
                        if entry['npc_id']!=cid or type(row.get('turn')) is not int or not 1<=row['turn']<=meta['turn']:
                            raise Error('Correction must identify its owner and actual acquisition turn')
                        if entry['evidence'] not in joined: raise Error('Correction needs evidence in audited source materials')
                        if not set(entry['source_ids'])<=set(meta['registry']): raise Error('Unknown correction source')
                        if entry['supersedes'] is not None and not 0<=entry['supersedes']<len(old):
                            raise Error('Correction must reference an existing earlier knowledge entry')
                elif not change['after'].startswith(current): raise Error('Append timeline corrections; preserve historical entries')
                files[name]=change['after']
            new=deepcopy(meta)
            new.update(version=meta['version']+1,packets={},audit_due=[])
            new['operations'].append({'kind':'audit','turn':meta['turn'],'scope':meta['audit_due'],'report':report,'corrections':corrections})
            self.transaction(path,meta,files,new)
            return self.status_of(new)

    def read_history(self,sid,start=1,end=None,query=''):
        with self.locked(sid) as (path,meta):
            rows=[row for row in self.active_history(path) if row['turn']>=start and (end is None or row['turn']<=end) and (not query or query.casefold() in (row['reader_text']+'\n'+row['narration']).casefold())]
            text=dump(rows)
            # Reuse lossless packets for arbitrarily large archive queries.
            pid=secrets.token_hex(16)
            chunks=[text[i:i+CHUNK_SIZE] for i in range(0,len(text),CHUNK_SIZE)]
            meta['packets'][pid]={'kind':'history','version':meta['version'],'payload':text,'chunk_size':CHUNK_SIZE,'hashes':[digest(c) for c in chunks],'delivered':[]}
            while len(meta['packets'])>4: del meta['packets'][next(iter(meta['packets']))]
            atomic(path/'system/meta.json',dump(meta))
            return {'packet_id':pid,'chunk_count':len(chunks),'chunk_size':CHUNK_SIZE,'chunk_hashes':[digest(c) for c in chunks],'sha256':digest(text),'turns':len(rows)}
