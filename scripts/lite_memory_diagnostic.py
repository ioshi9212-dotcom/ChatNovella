"""Read-only offline inventory for an EXISTING Lite session.

Point --root at a STOPPED service's storage or a consistent snapshot.
No session data is written, no conclusions about missing knowledge are invented.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

from chatnovella_lite.store import NPC, SID, _STATE_KNOWLEDGE, norm, parse_document


def inventory(root: Path, sid: str) -> dict:
    if not SID.fullmatch(sid):
        raise ValueError('Invalid Lite session ID')
    folder = root / sid
    if not folder.is_dir() or folder.is_symlink():
        raise FileNotFoundError(sid)
    meta = json.loads((folder / 'system/meta.json').read_text(encoding='utf-8'))
    state = parse_document((folder / 'state.md').read_text(encoding='utf-8'))
    timeline = (folder / 'timeline.md').read_text(encoding='utf-8')
    knowledge = {}
    for cid in meta['registry']:
        if cid != 'POV_001' and not NPC.fullmatch(cid):
            continue
        file = folder / 'knowledge' / (cid + '.md')
        knowledge[cid] = len(parse_document(file.read_text(encoding='utf-8'))) if file.exists() else None

    warnings = []
    def scan(value, where):
        if isinstance(value, str):
            if _STATE_KNOWLEDGE.search(value):
                warnings.append({'path': where, 'excerpt': value[:180]})
        elif isinstance(value, list):
            for i, item in enumerate(value): scan(item, f'{where}[{i}]')
        elif isinstance(value, dict):
            for key, item in value.items(): scan(item, f'{where}.{key}')
    for key in ('objects', 'unfinished', 'remote'):
        scan(state.get(key), 'state.' + key)
    scan(state.get('pov', {}).get('unfinished'), 'state.pov.unfinished')
    for cid, actor in state.get('present', {}).items():
        for key in ('unfinished', 'physical'):
            scan(actor.get(key), f'state.present.{cid}.{key}')

    milestones = []
    for match in re.finditer(r'```json\n(.*?)\n```',timeline,re.S):
        try: item=json.loads(match.group(1))
        except ValueError: continue
        if isinstance(item,dict) and 'event' in item:
            milestones.append((norm(item['event']),norm(item.get('consequence','')),item.get('date')))
    duplicated = sum(count - 1 for count in Counter(milestones).values() if count>1)

    turns = 0
    empty_memory_turns = 0
    incomplete_new_receipts = 0
    with (folder / 'history.jsonl').open(encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            row = json.loads(line)
            if row.get('kind') != 'turn': continue
            turns += 1
            if not row.get('knowledge'): empty_memory_turns += 1
            if 'knowledge_review' not in row: incomplete_new_receipts += 1

    return {
        'session_id': sid,
        'active_turn': meta['turn'],
        'archive_turn_records': turns,
        'personal_memory_count_by_id': knowledge,
        'npc_without_personal_file': sorted(k for k,v in knowledge.items() if v is None),
        'state_knowledge_like_entries': warnings,
        'timeline_event_count': len(milestones),
        'timeline_exact_duplicate_count': duplicated,
        'turn_records_without_knowledge': empty_memory_turns,
        'turn_records_before_receipt_protocol': incomplete_new_receipts,
        'note': 'This is a diagnostic, NOT a source-backed knowledge migration. Verify sources per NPC before repairs.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True,help='Consistent copy of Lite data root')
    parser.add_argument('--session-id',required=True)
    args = parser.parse_args()
    print(json.dumps(inventory(args.root,args.session_id),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
