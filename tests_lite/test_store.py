from copy import deepcopy
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch
from chatnovella_lite.store import Store, Error, digest, parse_document, dump, document

BASE=Path(__file__).resolve().parents[1]
REG={'POV_001':'Рина','NPC_001':'Рэй','NPC_002':'Джун','NPC_003':'Мики'}
ACTOR={'position':'у стола','clothing':'рубашка','footwear':'ботинки','items':['телефон'],'physical':'без травм','unfinished':[]}
def state():
    return {'turn':0,'datetime':'2026-09-06T21:00:00','location':'Библиотека','pov':deepcopy(ACTOR),
            'present':{'NPC_001':deepcopy(ACTOR)},'remote':{},'objects':['запертый шкаф'],'unfinished':['найти ключ']}
def relations():
    return {'rules':'Индивидуальные чувства, без обязательного роста.',
            'npc_to_pov':{'NPC_001':{'доверие':50,'ревность':0},'NPC_002':{'тревога':70}},
            'npc_to_npc':{'NPC_001':{'NPC_002':'Считает братом, не доверяет решениям'},'NPC_002':{'NPC_001':'Любит брата и скрывает письмо'}}}

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name,BASE)
        self.setup=self.store.create('Полная анкета. Рина пришла в библиотеку.','Полная анкета. Рина пришла в библиотеку.',REG,relations(),state())
        self.sid=self.setup['session_id']
        self.store.approve(self.sid,1,self.setup['canon_hash'],'Да, утверждаю')
    def tearDown(self): self.tmp.cleanup()
    def read(self,name): return (self.store.path(self.sid)/name).read_bytes().decode('utf-8')
    def packet(self,reader='Открой шкаф.',participants=None,kind='turn',read=True):
        p=self.store.prepare(self.sid,reader,participants or ['NPC_001'],kind)
        text=''
        if read:
            for i,h in enumerate(p['chunk_hashes']):
                c=self.store.read_chunk(self.sid,p['packet_id'],i)
                self.assertEqual(digest(c['text']),h)
                text+=c['text']
            self.assertEqual(digest(text),p['sha256'])
        return p,text
    def payload(self,reader='Открой шкаф.',narration='Рэй открыл шкаф. Внутри лежал ключ.',participants=None,**changes):
        p,_=self.packet(reader,participants)
        d={'request_id':'req-'+str(self.store.status(self.sid)['version']),'expected_version':p['version'],
           'packet_id':p['packet_id'],'chunk_hashes':p['chunk_hashes'],'reader_text':reader,'narration':narration,
           'participants':participants or ['NPC_001'],'state_patch':{'unfinished':[]},'relations':[],'knowledge':[],
           'events':[],'no_event_reason':'Только осмотр мебели без значимых событий.'}
        d.update(changes)
        for event in d['events']:
            event.setdefault('significance','major_consequence')
            event.setdefault('significance_reason','Это важное и долговременное изменение истории.')
        if 'knowledge_review' not in changes:
            owners={entry['npc_id'] for entry in d['knowledge']}
            d['knowledge_review']={cid:('recorded' if cid in owners else 'none')
                                   for cid in set(d['participants']) | owners}
        return d
    def audit(self):
        p,text=self.packet('',kind='audit')
        files=json.loads(text)['files']
        fields=['continuity','relationships','knowledge','time_state','timeline']
        if 60 in p['audit_due']: fields+=['all_npc_knowledge','long_term']
        quotes={'continuity':'Открой шкаф.','relationships':'доверие','knowledge':'Рэй','time_state':'Библиотека',
                'timeline':'Хронология игровых событий','all_npc_knowledge':'Джун','long_term':'Полная анкета.'}
        report={k:quotes[k] for k in fields}
        return self.store.finish_audit(self.sid,p['version'],p['packet_id'],p['chunk_hashes'],report,{})

    def test_full_canon_unicode_crlf_and_chunk_checksums(self):
        text=('Рина\r\nЛор 🖤, чужой секрет не известен NPC.\n'*2200)
        setup=self.store.create(text,text,REG,relations(),state())
        sid=setup['session_id']
        self.assertEqual((self.store.path(sid)/'original.md').read_bytes(),text.encode())
        self.store.approve(sid,1,setup['canon_hash'],'Утверждаю весь материал')
        p=self.store.prepare(sid,'Начать',['NPC_001'])
        self.assertGreater(p['chunk_count'],5)
        chunks=[self.store.read_chunk(sid,p['packet_id'],i)['text'] for i in range(p['chunk_count'])]
        payload=json.loads(''.join(chunks))
        self.assertTrue(payload['files']['novel.md'].startswith(text))
        self.assertNotIn('original.md',payload['files'])
        self.assertEqual(digest(''.join(chunks)),p['sha256'])
        self.assertEqual(set(x for x in payload['files'] if x.startswith('engine/')),{'engine/rules.md','engine/skin_builder.md'})

    def test_each_turn_reads_current_full_skin_builder(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Path(root)
            shutil.copytree(BASE/'engine',engine/'engine')
            self.store.engine=engine
            _,before=self.packet()
            previous=json.loads(before)['files']['engine/skin_builder.md']
            style=engine/'engine/skin_builder.md'
            self.assertEqual(previous,style.read_text())
            style.write_text('Актуальные новые правила стиля.\n')
            _,after=self.packet()
            self.assertEqual(json.loads(after)['files']['engine/skin_builder.md'],style.read_text())
            self.assertNotEqual(before,after)
            self.assertNotIn('generation_policy',json.loads(after))

    def test_setup_approval_canon_lock_and_explicit_amendment(self):
        s=self.store.create('А','Б',REG,relations(),state());sid=s['session_id']
        with self.assertRaises(Error): self.store.prepare(sid,'Начать',[])
        p=self.store.prepare(sid,'',[],'canon')
        txt=''.join(self.store.read_chunk(sid,p['packet_id'],i)['text'] for i in range(p['chunk_count']))
        self.assertEqual(json.loads(txt)['files']['original.md'],'А')
        with self.assertRaises(Error): self.store.approve(sid,1,s['canon_hash'],'Да')
        self.store.approve(sid,1,s['canon_hash'],'Да','Автор заменил А на Б')
        with self.assertRaises(Error): self.store.update_setup(sid,2,'В',relations(),state())
        self.store.amend_canon(sid,2,'Новая полная анкета','Разрешаю','Исправление автора')
        self.assertEqual((self.store.path(sid)/'original.md').read_text(),'А')
        meta=json.loads((self.store.path(sid)/'system/meta.json').read_text())
        self.assertIn('Б',meta['operations'][-1]['old_text'])

    def test_setup_can_be_corrected_before_approval(self):
        s=self.store.create('А','А',REG,relations(),state());sid=s['session_id']
        updated=self.store.update_setup(sid,1,'А\nДополнение',relations(),state())
        self.assertEqual(updated['version'],2)
        self.assertEqual((self.store.path(sid)/'original.md').read_text(),'А')

    def test_directional_relations_zero_delta_large_jump_and_new_scale(self):
        before=relations()
        self.store.commit(self.sid,**self.payload(relations=[{'source':'NPC_001','target':'POV_001','metric':'доверие','value':52,'reason':'Оказанная помощь.'}]))
        after=parse_document(self.read('relationships.md'))
        self.assertEqual(after['npc_to_pov']['NPC_001']['доверие'],52)
        self.assertEqual(after['npc_to_npc'],before['npc_to_npc'])
        d=self.payload(relations=[{'source':'NPC_001','target':'POV_001','metric':'доверие','value':70,'reason':'Просто так'}])
        with self.assertRaises(Error): self.store.commit(self.sid,**d)
        d['relations'][0].update(significant=True,reason='Спасла его жизнь.')
        self.store.commit(self.sid,**d)
        self.store.commit(self.sid,**self.payload(relations=[{'source':'NPC_001','target':'POV_001','metric':'ревность','value':None,'reason':'Нулевое чувство исчезло.'},
            {'source':'NPC_001','target':'POV_001','metric':'привязанность','value':10,'reason':'Возникла после совместного испытания.'}]))
        self.assertNotIn('ревность',parse_document(self.read('relationships.md'))['npc_to_pov']['NPC_001'])
        self.store.commit(self.sid,**self.payload())
        self.assertEqual(parse_document(self.read('relationships.md'))['npc_to_pov']['NPC_001']['доверие'],70)

    def test_unregistered_numbers_and_numeric_npc_to_npc_rejected(self):
        for change in [dict(source='NPC_999',target='POV_001',metric='злость',value=16,reason='Встреча'),
                       dict(source='NPC_001',target='NPC_002',value='16/+1',reason='Разговор')]:
            with self.assertRaises(Error): self.store.commit(self.sid,**self.payload(relations=[change]))

    def test_memory_private_owner_phone_mistake_and_refutation(self):
        quote='Рэй услышал по телефону: Джун ушёл.'
        d=self.payload(narration=quote,knowledge=[dict(npc_id='NPC_001',fact='Джун ушёл.',status='mistaken',source_kind='phone',source_ids=['NPC_002'],source='Джун сказал в звонке.',evidence=quote)])
        self.store.commit(self.sid,**d)
        self.assertEqual(parse_document(self.read('knowledge/NPC_002.md')),[])
        old=self.read('knowledge/NPC_001.md')
        self.store.commit(self.sid,**self.payload(narration='Рэй увидел Джуна в дверях.',knowledge=[dict(npc_id='NPC_001',fact='Джун не ушёл.',status='refuted',source_kind='witnessed',source_ids=['NPC_002'],source='Лично увидел его.',evidence='Рэй увидел Джуна в дверях.',supersedes=0)]))
        entries=parse_document(self.read('knowledge/NPC_001.md'))
        self.assertEqual([e['status'] for e in entries],['mistaken','refuted'])
        self.assertEqual(entries[1]['supersedes'],0)

    def test_absent_owner_and_missing_source_rejected(self):
        entry=dict(npc_id='NPC_002',fact='Секрет Рины.',status='knows',source_kind='told',source_ids=['POV_001'],source='Якобы услышал.',evidence='Рэй открыл шкаф.')
        with self.assertRaises(Error): self.store.commit(self.sid,**self.payload(knowledge=[entry]))
        entry['npc_id']='NPC_001';entry['evidence']='Этого нет в сцене'
        with self.assertRaises(Error): self.store.commit(self.sid,**self.payload(knowledge=[entry]))

    def test_entering_npc_must_be_loaded_and_offscreen_source_required(self):
        d=self.payload();d['participants'].append('NPC_002')
        with self.assertRaises(Error): self.store.commit(self.sid,**d)
        d=self.payload(participants=['NPC_001','NPC_002'])
        d['state_patch']={'remote':{'NPC_002':'телефон'}}
        self.store.commit(self.sid,**d)
        e=dict(npc_id='NPC_003',fact='Поезд отменён.',status='knows',source_kind='offscreen',source_ids=[],source='Прочитал табло.',evidence='Мики прочитал табло',event_index=0)
        d=self.payload(participants=['NPC_001','NPC_002','NPC_003'],knowledge=[e],events=[dict(date=None,participants=['NPC_003'],event='Мики прочитал табло отмены поезда.',consequence='Узнал об отмене.')])
        self.store.commit(self.sid,**d)
        self.assertIn('Поезд отменён',self.read('knowledge/NPC_003.md'))

    def test_state_patch_preserves_clothes_items_and_departure(self):
        self.store.commit(self.sid,**self.payload(state_patch={'pov':{'position':'у окна'},'present':{}}))
        st=parse_document(self.read('state.md'))
        self.assertEqual(st['pov']['clothing'],'рубашка')
        self.assertEqual(st['pov']['items'],['телефон'])
        self.assertEqual(st['present'],{})

    def test_timeline_append_significant_only_and_unknown_date(self):
        before=self.read('timeline.md')
        self.store.commit(self.sid,**self.payload())
        self.assertEqual(self.read('timeline.md'),before)
        self.store.commit(self.sid,**self.payload(events=[dict(date=None,participants=['POV_001','NPC_001'],event='Найден ключ от архива.',consequence='Можно открыть архив.')]))
        self.assertTrue(self.read('timeline.md').startswith(before))
        self.assertIn('"date": null',self.read('timeline.md'))

    def test_failed_write_restores_every_file_and_retry_succeeds(self):
        before={p.relative_to(self.store.path(self.sid)).as_posix():p.read_bytes() for p in self.store.path(self.sid).glob('*.md')}
        d=self.payload()
        actual=self.store.materialize
        def fail(path,files):
            first=next(iter(files))
            actual(path,{first:files[first]})
            raise OSError('simulated disk write failure')
        with patch.object(self.store,'materialize',fail):
            with self.assertRaises(OSError): self.store.commit(self.sid,**d)
        self.assertEqual(self.store.status(self.sid)['turn'],0)
        for name,content in before.items(): self.assertEqual((self.store.path(self.sid)/name).read_bytes(),content)
        self.assertEqual(self.read('history.jsonl'),'')
        self.assertTrue(self.store.commit(self.sid,**d)['saved'])

    def test_process_interruption_recovers_complete_commit_on_restart(self):
        d=self.payload()
        actual=self.store.materialize
        def kill(path,files):
            actual(path,{next(iter(files)):next(iter(files.values()))})
            raise KeyboardInterrupt('simulated process death')
        with patch.object(self.store,'materialize',kill):
            with self.assertRaises(KeyboardInterrupt): self.store.commit(self.sid,**d)
        restarted=Store(self.tmp.name,BASE)
        result=restarted.commit(self.sid,**d)
        self.assertTrue(result['saved'])
        self.assertEqual(restarted.status(self.sid)['turn'],1)
        self.assertEqual(len(restarted.active_history(restarted.path(self.sid))),1)

    def test_idempotency_stale_receipt_and_rollback_preserves_archive(self):
        d=self.payload();saved=self.store.commit(self.sid,**d)
        self.assertEqual(self.store.commit(self.sid,**d),saved)
        with self.assertRaises(Error): self.store.commit(self.sid,**{**d,'narration':'Другой текст'})
        before=self.store.status(self.sid)
        self.store.rollback(self.sid,before['version'],d['request_id'],'Автор отменяет ход.')
        self.assertEqual(self.store.status(self.sid)['turn'],0)
        self.assertIn('Рэй открыл шкаф.',self.read('history.jsonl'))
        self.assertEqual(self.store.active_history(self.store.path(self.sid)),[])
        with self.assertRaises(Error): self.store.commit(self.sid,**d)

    def test_incomplete_context_or_stale_version_blocks_commit(self):
        d=self.payload();p,_=self.packet(read=False);d['packet_id']=p['packet_id'];d['chunk_hashes']=p['chunk_hashes']
        with self.assertRaises(Error): self.store.commit(self.sid,**d)
        d=self.payload();d['expected_version']=1
        with self.assertRaises(Error): self.store.commit(self.sid,**d)

    def test_two_sessions_restart_ids_and_tampering(self):
        other=self.store.create('Другая история','Другая история',REG,relations(),state())
        self.store.commit(self.sid,**self.payload())
        restart=Store(self.tmp.name,BASE)
        self.assertEqual(restart.status(self.sid)['registry'],REG)
        self.assertEqual(restart.status(other['session_id'])['turn'],0)
        (self.store.path(self.sid)/'novel.md').write_text('Молча подменённый канон')
        with self.assertRaises(Error): restart.status(self.sid)
        with self.assertRaises(Error): restart.status('../other')

    def test_60_turns_audit_boundaries_full_scenes_all_npc_and_archive_search(self):
        # Deliberately deterministic server endurance fixture, not a claim of
        # 60 turns written by GPT through a live ChatGPT connection.
        for i in range(1,61):
            d=self.payload(narration=f'Полная сцена {i}. Рэй прочитал страницу архива.',participants=['NPC_001','NPC_003'] if i==1 else ['NPC_001'])
            if i==1:
                d['participants']=['NPC_001']
                d['events']=[dict(date=None,participants=['NPC_003'],event='Мики прочитал табло отмены поезда.',consequence='Узнал об отмене.',significance='major_consequence',significance_reason='Отмена меняет запланированное путешествие героев.')]
                d['knowledge']=[dict(npc_id='NPC_003',fact='Поезд отменён.',status='knows',source_kind='offscreen',source_ids=[],source='Прочитал табло.',evidence='Мики прочитал табло',event_index=0)]
                d['knowledge_review']['NPC_003']='recorded'
            self.store.commit(self.sid,**d)
            if i%15==0:
                with self.assertRaises(Error): self.packet()
                p,text=self.packet('',kind='audit')
                data=json.loads(text)
                scenes=json.loads(data['files']['recent_scenes.json'])
                self.assertEqual(len(scenes),15)
                self.assertEqual(scenes[0]['turn'],i-14)
                if i==30: self.assertNotIn('knowledge/NPC_003.md',data['files'])  # owner not in this audit's 15-turn scope
                if i==60:
                    self.assertEqual(set(p['participants']),set(REG)-{'POV_001'})
                    self.assertIn('Поезд отменён.',data['files']['knowledge/NPC_003.md'])
                with self.assertRaises(Error): self.store.finish_audit(self.sid,p['version'],p['packet_id'],p['chunk_hashes'],{}, {})
                self.audit()
        st=self.store.status(self.sid)
        self.assertEqual(st['turn'],60)
        self.assertEqual(st['audit_due'],[])
        p=self.store.read_history(self.sid,2,59,'Полная сцена')
        text=''.join(self.store.read_chunk(self.sid,p['packet_id'],i)['text'] for i in range(p['chunk_count']))
        self.assertEqual(len(json.loads(text)),58)
        meta=json.loads((self.store.path(self.sid)/'system/meta.json').read_text())
        self.assertEqual([o['turn'] for o in meta['operations'] if o['kind']=='audit'],[15,30,45,60])

    def test_rollback_restores_all_data_including_new_memory(self):
        names=['state.md','relationships.md','timeline.md']
        old={name:self.read(name) for name in names}
        d=self.payload(narration='Рэй увидел ключ от архива.',
            state_patch={'pov':{'items':['телефон','ключ']}},
            relations=[dict(source='NPC_001',target='POV_001',metric='доверие',value=53,reason='Получил ключ.')],
            knowledge=[dict(npc_id='NPC_001',fact='Ключ найден.',status='knows',source_kind='witnessed',source_ids=['POV_001'],source='Увидел лично.',evidence='Рэй увидел ключ от архива.')],
            events=[dict(date='2026-09-06',participants=['POV_001','NPC_001'],event='Рэй увидел ключ от архива.',consequence='Архив можно открыть.')])
        saved=self.store.commit(self.sid,**d)
        self.store.rollback(self.sid,saved['version'],d['request_id'],'Вернуться к прежнему варианту.')
        for name in names: self.assertEqual(self.read(name),old[name])
        self.assertEqual(parse_document(self.read('knowledge/NPC_001.md')),[])
        self.assertEqual(len(self.store.history(self.store.path(self.sid))),2)

    def test_concurrent_requests_one_version_only_one_turn(self):
        from concurrent.futures import ThreadPoolExecutor
        first=self.payload();second=deepcopy(first);second['request_id']='parallel-other'
        def commit(d):
            try: return self.store.commit(self.sid,**d)['saved']
            except Error: return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(commit,[first,second]))
        self.assertEqual(sum(result),1)
        self.assertEqual(self.store.status(self.sid)['turn'],1)

    def test_audit_requires_all_chunks_and_preserves_canon_history(self):
        for i in range(15): self.store.commit(self.sid,**self.payload())
        p,_=self.packet('',kind='audit',read=False)
        report={k:'Полная анкета.' for k in ['continuity','relationships','knowledge','time_state','timeline']}
        with self.assertRaises(Error): self.store.finish_audit(self.sid,p['version'],p['packet_id'],p['chunk_hashes'],report,{})
        for i in range(p['chunk_count']): self.store.read_chunk(self.sid,p['packet_id'],i)
        with self.assertRaises(Error): self.store.finish_audit(self.sid,p['version'],p['packet_id'],p['chunk_hashes'],report,{'novel.md':{'before':self.read('novel.md'),'after':'Подмена','reason':'Нельзя'}})
        before=self.read('timeline.md');after=before+'\nУточнение: ключ остался на столе. Источник: ход 15.\n'
        self.store.finish_audit(self.sid,p['version'],p['packet_id'],p['chunk_hashes'],report,{'timeline.md':{'before':before,'after':after,'reason':'Уточнение по полной сцене 15.'}})
        self.assertEqual(self.store.status(self.sid)['audit_due'],[])
        self.assertEqual(self.read('timeline.md'),after)
        self.assertEqual(len(self.store.active_history(self.store.path(self.sid))),15)

    def test_backward_time_is_rejected(self):
        with self.assertRaises(Error): self.store.commit(self.sid,**self.payload(state_patch={'datetime':'2026-09-05T20:00:00'}))

    def test_audit_knowledge_correction_is_sourced_append_only(self):
        quote='Рэй увидел ключ от архива.'
        self.store.commit(self.sid,**self.payload(narration=quote,knowledge=[dict(npc_id='NPC_001',fact='Найден ключ.',status='suspects',source_kind='witnessed',source_ids=['POV_001'],source='Видел лично.',evidence=quote)]))
        for _ in range(14): self.store.commit(self.sid,**self.payload())
        p,_=self.packet('',kind='audit')
        report={k:'Полная анкета.' for k in ['continuity','relationships','knowledge','time_state','timeline']}
        before=self.read('knowledge/NPC_001.md');entries=parse_document(before)
        correction={**entries[0],'status':'knows','supersedes':0}
        after=document('Приобретённые знания',entries+[correction])
        bad=deepcopy(correction);bad['npc_id']='NPC_002'
        with self.assertRaises(Error): self.store.finish_audit(self.sid,p['version'],p['packet_id'],p['chunk_hashes'],report,{'knowledge/NPC_001.md':{'before':before,'after':document('Приобретённые знания',entries+[bad]),'reason':'Уточнение.'}})
        self.store.finish_audit(self.sid,p['version'],p['packet_id'],p['chunk_hashes'],report,{'knowledge/NPC_001.md':{'before':before,'after':after,'reason':'Факт подтверждён сценой 1.'}})
        self.assertEqual(parse_document(self.read('knowledge/NPC_001.md'))[0],entries[0])
        self.assertEqual(len(parse_document(self.read('knowledge/NPC_001.md'))),2)

    def test_new_permanent_npc_requires_explicit_canon_amendment(self):
        old=self.store.status(self.sid)
        updated=self.store.amend_canon(self.sid,old['version'],'Полная анкета. Рина пришла в библиотеку. Новый архивариус: Кай.','Разрешаю добавить Кая.','Новый постоянный персонаж.',{'NPC_004':'Кай'})
        self.assertEqual(updated['registry']['NPC_004'],'Кай')
        self.assertEqual(parse_document(self.read('knowledge/NPC_004.md')),[])
        for cid,name in REG.items(): self.assertEqual(updated['registry'][cid],name)
        with self.assertRaises(Error): self.store.amend_canon(self.sid,updated['version'],'Другой канон.','Да','Переназначение',{'NPC_001':'Другой Рэй'})
        restarted=Store(self.tmp.name,BASE)
        self.assertEqual(restarted.status(self.sid)['registry']['NPC_004'],'Кай')

    def test_16k_chunks_and_existing_12k_packet_survive_rollout(self):
        p,_=self.packet(read=False)
        self.assertEqual(p['chunk_size'],16000)
        first=self.store.read_chunk(self.sid,p['packet_id'],0)
        self.assertEqual(len(first['text']),16000)
        meta_path=self.store.path(self.sid)/'system/meta.json'
        meta=json.loads(meta_path.read_text());packet=meta['packets'][p['packet_id']]
        packet.pop('chunk_size')
        text=packet['payload']
        legacy=[text[i:i+12000] for i in range(0,len(text),12000)]
        packet['hashes']=[digest(c) for c in legacy];packet['delivered']=[]
        meta_path.write_text(dump(meta))
        restored=''
        for i,expected in enumerate(legacy):
            c=self.store.read_chunk(self.sid,p['packet_id'],i)
            self.assertEqual(c['text'],expected)
            self.assertEqual(c['sha256'],digest(expected))
            restored+=c['text']
        self.assertEqual(restored,text)
